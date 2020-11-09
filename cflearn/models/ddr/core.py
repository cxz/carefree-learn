import torch

import torch.nn as nn

from torch import Tensor
from typing import Any
from typing import Dict
from typing import List
from typing import Union
from typing import Callable
from typing import Optional
from functools import partial
from cftool.misc import context_error_handler

from ...types import tensor_dict_type
from ...types import tensor_tuple_type
from ...misc.toolkit import switch_requires_grad
from ...modules.blocks import MLP
from ...modules.blocks import InvertibleBlock
from ...modules.blocks import MonotonousMapping
from ...modules.blocks import ConditionalBlocks
from ...modules.blocks import ConditionalOutput
from ...modules.blocks import PseudoInvertibleBlock


def transition_builder(dim: int) -> nn.Module:
    h_dim = int(dim // 2)
    return MonotonousMapping.make_couple(h_dim, h_dim, h_dim, "sigmoid", ascent=True)


def get_cond_mappings(
    condition_dim: int,
    cond_out_dim: int,
    num_units: List[int],
    bias: bool,
) -> nn.ModuleList:
    cond_module = MLP.simple(
        condition_dim,
        cond_out_dim,
        num_units,
        bias=bias,
        activation="mish",
    )
    return cond_module.mappings


class MonoSplit(nn.Module):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        ascent1: bool,
        ascent2: bool,
        to_latent: bool,
        builder: Callable[[int, int, bool], ConditionalBlocks],
        builder2: Optional[Callable[[int, int, bool], ConditionalBlocks]] = None,
    ) -> None:
        super().__init__()
        if builder2 is None:
            builder2 = builder
        self.to_latent = to_latent
        self.m1 = builder(in_dim, out_dim, ascent1)
        self.m2 = builder2(in_dim, out_dim, ascent2)

    def forward(
        self,
        net: Union[Tensor, tensor_tuple_type],
        cond: Tensor,
    ) -> Union[Tensor, tensor_tuple_type, ConditionalOutput]:
        if self.to_latent:
            assert isinstance(net, Tensor)
            return self.m1(net, cond), self.m2(net, cond)
        assert isinstance(net, tuple)
        o1, o2 = self.m1(net[0], cond), self.m2(net[1], cond)
        return ConditionalOutput(o1.net + o2.net, o1.cond + o2.cond)


def cond_transform_fn(net: Tensor, cond: Tensor) -> Tensor:
    return torch.tanh(2.0 * net) * cond


def monotonous_builder(
    ascent1: bool,
    ascent2: bool,
    to_latent: bool,
    num_layers: int,
    condition_dim: int,
    invertible_module: PseudoInvertibleBlock = None,
) -> Callable[[int, int], nn.Module]:
    def _core(
        in_dim: int,
        out_dim: int,
        ascent: bool,
        cond_mappings: Optional[nn.ModuleList] = None,
    ) -> ConditionalBlocks:
        cond_out_dim: Optional[int]
        block_out_dim: Optional[int]
        if to_latent:
            num_units = [out_dim] * (num_layers + 1)
            cond_out_dim = block_out_dim = None
        else:
            num_units = [in_dim] * num_layers
            # cond  : median, pos_median_res, neg_median_res
            assert out_dim == 3
            cond_out_dim = out_dim
            # block : cdf
            if cond_mappings is None:
                block_out_dim = 1
            # block : y_add, y_mul
            else:
                block_out_dim = 2

        blocks = MonotonousMapping.stack(
            in_dim,
            block_out_dim,
            num_units,
            ascent=ascent,
            use_couple_bias=False,
            activation="sigmoid" if to_latent else "tanh",
            return_blocks=True,
        )
        assert isinstance(blocks, list)

        if cond_mappings is None or len(cond_mappings) == 0:
            cond_mappings = get_cond_mappings(
                condition_dim,
                cond_out_dim,
                num_units,
                not to_latent,
            )

        return ConditionalBlocks(
            nn.ModuleList(blocks),
            cond_mappings,
            add_last=to_latent,
            detach_condition=not to_latent,
            cond_transform_fn=cond_transform_fn,
        )

    def _split_core(in_dim: int, out_dim: int) -> nn.Module:
        if not to_latent:
            in_dim = int(in_dim // 2)
        else:
            out_dim = int(out_dim // 2)
        if invertible_module is None:
            return MonoSplit(in_dim, out_dim, ascent1, ascent2, to_latent, _core)
        # reuse MonoSplit parameters
        from_latent1 = invertible_module.from_latent
        if isinstance(from_latent1, nn.Identity):
            cond_blocks1 = cond_blocks2 = []
        else:
            m1 = from_latent1.m1
            m2 = invertible_module.from_latent.m2
            cond_blocks1 = m1.condition_blocks
            cond_blocks2 = m2.condition_blocks
        b1 = partial(_core, cond_mappings=cond_blocks1)
        b2 = partial(_core, cond_mappings=cond_blocks2)
        return MonoSplit(in_dim, out_dim, ascent1, ascent2, to_latent, b1, b2)

    return _split_core


class DDRCore(nn.Module):
    def __init__(
        self,
        in_dim: int,
        fetch_q: bool,
        fetch_cdf: bool,
        num_layers: Optional[int] = None,
        num_blocks: Optional[int] = None,
        latent_dim: Optional[int] = None,
    ):
        super().__init__()
        # common
        if not fetch_q and not fetch_cdf:
            raise ValueError("something must be fetched, either `q` or `cdf`")
        self.fetch_q = fetch_q
        self.fetch_cdf = fetch_cdf
        if num_layers is None:
            num_layers = 1
        if num_blocks is None:
            num_blocks = 2
        if num_blocks % 2 != 0:
            raise ValueError("`num_blocks` should be divided by 2")
        if latent_dim is None:
            latent_dim = 512
        self.latent_dim = latent_dim
        # pseudo invertible q
        kwargs = {"num_layers": num_layers, "condition_dim": in_dim}
        if not self.fetch_q:
            q_to_latent_builder = self.dummy_builder
        else:
            q_to_latent_builder = monotonous_builder(
                ascent1=True,
                ascent2=True,
                to_latent=True,
                **kwargs,
            )
        if not self.fetch_cdf:
            q_from_latent_builder = self.dummy_builder
        else:
            q_from_latent_builder = monotonous_builder(
                ascent1=True,
                ascent2=False,
                to_latent=False,
                **kwargs,
            )
        self.q_invertible = PseudoInvertibleBlock(
            1,
            latent_dim,
            3,
            to_transition_builder=q_to_latent_builder,
            from_transition_builder=q_from_latent_builder,
        )
        # pseudo invertible y
        if not self.fetch_cdf:
            y_to_latent_builder = self.dummy_builder
        else:
            y_to_latent_builder = monotonous_builder(
                ascent1=True,
                ascent2=False,
                to_latent=True,
                **kwargs,
            )
        if not self.fetch_q:
            y_from_latent_builder = self.dummy_builder
        else:
            y_from_latent_builder = monotonous_builder(
                ascent1=True,
                ascent2=True,
                to_latent=False,
                invertible_module=self.q_invertible,
                **kwargs,
            )
        self.y_invertible = PseudoInvertibleBlock(
            1,
            latent_dim,
            3,
            to_transition_builder=y_to_latent_builder,
            from_transition_builder=y_from_latent_builder,
        )
        if self.fetch_q and self.fetch_cdf:
            q_cond_blocks1 = self.q_invertible.from_latent.m1.condition_blocks
            q_cond_blocks2 = self.q_invertible.from_latent.m2.condition_blocks
            y_cond_blocks1 = self.y_invertible.from_latent.m1.condition_blocks
            y_cond_blocks2 = self.y_invertible.from_latent.m2.condition_blocks
            assert q_cond_blocks1 is y_cond_blocks1
            assert q_cond_blocks2 is y_cond_blocks2
        # q parameters
        if not self.fetch_q:
            self.q_parameters = []
        else:
            q_params1 = list(self.q_invertible.to_latent.parameters())
            q_params2 = list(self.y_invertible.from_latent.parameters())
            self.q_parameters = q_params1 + q_params2
        # invertible blocks
        self.num_blocks = num_blocks
        self.block_parameters: List[nn.Parameter] = []
        self.blocks = nn.ModuleList()
        for _ in range(num_blocks):
            block = InvertibleBlock(latent_dim, transition_builder=transition_builder)
            self.block_parameters.extend(block.parameters())
            self.blocks.append(block)

    @property
    def q_fn(self) -> Callable[[Tensor], Tensor]:
        return lambda q: 2.0 * q - 1.0

    @property
    def q_inv_fn(self) -> Callable[[Tensor], Tensor]:
        return torch.sigmoid

    @property
    def dummy_builder(self) -> Callable[[int, int], nn.Module]:
        return lambda _, __: nn.Identity()

    def _detach_q(self) -> context_error_handler:
        fetch_q = self.fetch_q

        def switch(requires_grad: bool) -> None:
            switch_requires_grad(self.q_parameters, requires_grad)
            switch_requires_grad(self.block_parameters, requires_grad)

        class _(context_error_handler):
            def __enter__(self) -> None:
                switch(not fetch_q)

            def _normal_exit(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                switch(True)

        return _()

    @staticmethod
    def _get_median_outputs(outputs: ConditionalOutput) -> tensor_dict_type:
        cond_net = outputs.cond
        med, pos_med_res, neg_med_res = cond_net.split(1, dim=1)
        return {"median": med, "pos_med_res": pos_med_res, "neg_med_res": neg_med_res}

    @staticmethod
    def _merge_q_outputs(
        q_batch: Tensor,
        outputs: ConditionalOutput,
        median_outputs: tensor_dict_type,
    ) -> tensor_dict_type:
        pos_med_res = median_outputs["pos_med_res"]
        neg_med_res = median_outputs["neg_med_res"]
        q_positive_mask = torch.sign(q_batch) == 1.0
        med_res = torch.where(q_positive_mask, pos_med_res, neg_med_res)
        y_add, y_mul = outputs.net.split(1, dim=1)
        y_res = med_res.detach() * y_mul + y_add
        return {
            "y_res": y_res,
            "med_add": y_add,
            "med_mul": y_mul,
            "med_res": med_res,
            "q_positive_mask": q_positive_mask,
        }

    def _median_results(self, net: Tensor) -> tensor_dict_type:
        dummy1 = dummy2 = net.new_zeros(len(net), self.latent_dim // 2)
        if not self.fetch_q:
            inverse_method = self.q_invertible.inverse
        else:
            inverse_method = self.y_invertible.inverse
        pack = inverse_method((dummy1, dummy2), net)
        assert isinstance(pack, ConditionalOutput)
        return self._get_median_outputs(pack)

    def _q_results(
        self,
        net: Tensor,
        q_batch: Tensor,
        do_inverse: bool = False,
    ) -> tensor_dict_type:
        # prepare q_latent
        q_batch = self.q_fn(q_batch)
        q1, q2 = self.q_invertible(q_batch, net)
        q1, q2 = q1.net, q2.net
        # simulate quantile function
        for block in self.blocks:
            q1, q2 = block(q1, q2)
        y_pack = self.y_invertible.inverse((q1, q2), net)
        assert isinstance(y_pack, ConditionalOutput)
        median_outputs = self._get_median_outputs(y_pack)
        results = self._merge_q_outputs(q_batch, y_pack, median_outputs)
        q_inverse = None
        if do_inverse and self.fetch_cdf:
            y_res = results["y_res"]
            assert y_res is not None
            inverse_results = self._y_results(net, y_res.detach())
            q_inverse = inverse_results["q"]
        results["q_inverse"] = q_inverse
        return results

    def _y_results(
        self,
        net: Tensor,
        median_residual: Tensor,
        do_inverse: bool = False,
    ) -> tensor_dict_type:
        # prepare y_latent
        y1, y2 = self.y_invertible(median_residual, net)
        y1, y2 = y1.net, y2.net
        # simulate cdf
        with self._detach_q():
            for i in range(self.num_blocks):
                y1, y2 = self.blocks[self.num_blocks - i - 1].inverse(y1, y2)
            q_logit_pack = self.q_invertible.inverse((y1, y2), net)
            assert isinstance(q_logit_pack, ConditionalOutput)
            q_logit = q_logit_pack.net
            q = self.q_inv_fn(q_logit)
            y_inverse_res = None
            if do_inverse and self.fetch_q:
                inverse_results = self._q_results(net, q.detach())
                y_inverse_res = inverse_results["y_res"]
        return {"q": q, "q_logit": q_logit, "y_inverse_res": y_inverse_res}

    def forward(
        self,
        net: Tensor,
        *,
        median: bool = False,
        do_inverse: bool = False,
        q_batch: Optional[Tensor] = None,
        y_batch: Optional[Tensor] = None,
    ) -> tensor_dict_type:
        results = self._median_results(net)
        if self.fetch_q and not median and q_batch is not None:
            results.update(self._q_results(net, q_batch, do_inverse))
        if self.fetch_cdf and not median and y_batch is not None:
            median_residual = y_batch - results["median"].detach()
            results.update(self._y_results(net, median_residual, do_inverse))
        return results


__all__ = ["DDRCore"]
