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

from ...types import tensor_tuple_type
from ...misc.toolkit import switch_requires_grad
from ...modules.blocks import MLP
from ...modules.blocks import InvertibleBlock
from ...modules.blocks import MonotonousMapping
from ...modules.blocks import PseudoInvertibleBlock


class DDRCore(nn.Module):
    def __init__(
        self,
        in_dim: int,
        y_min: float,
        y_max: float,
        num_blocks: Optional[int] = None,
        latent_dim: Optional[int] = None,
        latent_builder: Optional[Callable[[int, int], nn.Module]] = None,
        transition_builder: Optional[Callable[[int], nn.Module]] = None,
        q_to_latent_builder: Optional[Callable[[int, int], nn.Module]] = None,
        q_from_latent_builder: Optional[Callable[[int, int], nn.Module]] = None,
        y_to_latent_builder: Optional[Callable[[int, int], nn.Module]] = None,
        y_from_latent_builder: Optional[Callable[[int, int], nn.Module]] = None,
    ):
        super().__init__()
        self.y_min = y_min
        self.y_diff = y_max - y_min
        mono_activation = "Tanh"
        # builders
        def default_latent_builder(in_dim_: int, latent_dim_: int) -> nn.Module:
            return MLP.simple(
                in_dim_,
                None,
                [latent_dim_, latent_dim_],
                activation="mish",
            )

        if latent_builder is None:
            latent_builder = default_latent_builder

        def get_monotonous_builder(
            ascents: Union[bool, List[bool]],
            ascent_split: Optional[str],  # "input" or "output"
        ) -> Callable[[int, int], nn.Module]:
            if isinstance(ascents, bool):
                ascents = [ascents]
            split_input = ascent_split == "input"

            def _core(in_dim_: int, out_dim_: int, ascent: bool) -> nn.Sequential:
                true_out_dim_: Optional[int]
                if split_input:
                    num_units = [in_dim_]
                    true_out_dim_ = out_dim_
                else:
                    num_units = [out_dim_, out_dim_]
                    true_out_dim_ = None

                return MonotonousMapping.stack(
                    in_dim_,
                    true_out_dim_,
                    num_units,
                    ascent=ascent,
                )

            if len(ascents) == 1:
                return partial(_core, ascent=ascents[0])

            assert len(ascents) == 2, "currently only split in half is supported"

            def _split_core(in_dim_: int, out_dim_: int) -> nn.Module:
                assert isinstance(ascents, list)
                if split_input:
                    in_dim_ = int(in_dim_ // len(ascents))
                else:
                    out_dim_ = int(out_dim_ // len(ascents))
                net_type = Union[Tensor, tensor_tuple_type]

                class MonoSplit(nn.Module):
                    def __init__(self) -> None:
                        super().__init__()
                        assert isinstance(ascents, list)
                        self.m1 = _core(in_dim_, out_dim_, ascents[0])
                        self.m2 = _core(in_dim_, out_dim_, ascents[1])

                    def forward(self, net: net_type) -> net_type:
                        if not split_input:
                            return self.m1(net), self.m2(net)
                        return self.m1(net[0]) + self.m2(net[1])

                return MonoSplit()

            return _split_core

        # to latent
        if latent_dim is None:
            latent_dim = 512
        assert latent_builder is not None
        self.to_latent = latent_builder(in_dim, latent_dim)
        # pseudo invertible q / y
        if q_to_latent_builder is None:
            q_to_latent_builder = get_monotonous_builder(True, "output")
        if q_from_latent_builder is None:
            q_from_latent_builder = get_monotonous_builder([True, False], "input")
        self.q_invertible = PseudoInvertibleBlock(
            1,
            latent_dim,
            to_transition_builder=q_to_latent_builder,
            from_transition_builder=q_from_latent_builder,
        )
        if y_to_latent_builder is None:
            y_to_latent_builder = get_monotonous_builder([True, False], "output")
        if y_from_latent_builder is None:
            y_from_latent_builder = get_monotonous_builder(True, "input")
        self.y_invertible = PseudoInvertibleBlock(
            1,
            latent_dim,
            to_transition_builder=y_to_latent_builder,
            from_transition_builder=y_from_latent_builder,
        )
        q_params1 = list(self.q_invertible.to_latent.parameters())
        q_params2 = list(self.y_invertible.from_latent.parameters())
        self.q_parameters = q_params1 + q_params2
        # transition builder
        def default_transition_builder(dim: int) -> nn.Module:
            h_dim = int(dim // 2)
            return MonotonousMapping(
                h_dim,
                h_dim,
                ascent=True,
                activation=mono_activation,
            )

        if transition_builder is None:
            transition_builder = default_transition_builder
        # invertible blocks
        if num_blocks is None:
            num_blocks = 2
        if num_blocks % 2 != 0:
            raise ValueError("`num_blocks` should be divided by 2")
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
    def y_fn(self) -> Callable[[Tensor], Tensor]:
        return lambda y: (y - self.y_min) / (0.5 * self.y_diff) - 1.0

    @property
    def q_inv_fn(self) -> Callable[[Tensor], Tensor]:
        return torch.sigmoid

    @property
    def y_inv_fn(self) -> Callable[[Tensor], Tensor]:
        return lambda y: (y + 1.0) * (0.5 * self.y_diff) + self.y_min

    def _detach_q(self) -> context_error_handler:
        def switch(requires_grad: bool) -> None:
            switch_requires_grad(self.q_parameters, requires_grad)
            switch_requires_grad(self.block_parameters, requires_grad)

        class _(context_error_handler):
            def __enter__(self) -> None:
                switch(False)

            def _normal_exit(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                switch(True)

        return _()

    def _get_q_results(
        self,
        net: Tensor,
        l1: Tensor,
        l2: Tensor,
        q_batch: Optional[Tensor] = None,
        auto_encode: bool = False,
        do_inverse: bool = False,
        median: bool = False,
    ) -> Dict[str, Optional[Tensor]]:
        # prepare q_latent
        if q_batch is not None:
            q_batch = self.q_fn(q_batch)
        elif median:
            if q_batch is not None:
                msg = "`median` is specified but `q_batch` is still provided"
                raise ValueError(msg)
            q_batch = net.new_zeros(len(net), 1)
        if q_batch is None:
            q1 = q2 = q_latent = None
        else:
            q_latent = self.q_invertible(q_batch)
            if not isinstance(q_latent, tuple):
                q1, q2 = q_latent.chunk(2, dim=1)
            else:
                q1, q2 = q_latent
                q_latent = torch.cat(q_latent, dim=1)
        # simulate quantile function
        q_ae = q_inverse = None
        y_inverse_latent = yq_inverse_latent = None
        if q_latent is None:
            y = qy_latent = None
        else:
            assert q1 is not None and q2 is not None
            if auto_encode:
                q_ae_logit = self.q_invertible.inverse((q1, q2))
                q_ae = self.q_inv_fn(q_ae_logit)
            q1, q2 = q1 + l1, q2 + l2
            for block in self.blocks:
                q1, q2 = block(q1, q2)
            qy_latent = torch.cat([q1, q2], dim=1)
            y = self.y_invertible.inverse(qy_latent)
            y = self.y_inv_fn(y)
            if do_inverse:
                inverse_results = self.forward(
                    net,
                    l1.detach(),
                    l2.detach(),
                    y_batch=y.detach(),
                    do_inverse=False,
                )
                q_inverse = inverse_results["q"]
                y_inverse_latent = inverse_results["y_latent"]
                yq_inverse_latent = inverse_results["yq_latent"]
        return {
            "y": y,
            "q_ae": q_ae,
            "q_latent": q_latent,
            "qy_latent": qy_latent,
            "q_inverse": q_inverse,
            "y_inverse_latent": y_inverse_latent,
            "yq_inverse_latent": yq_inverse_latent,
        }

    def _get_y_results(
        self,
        net: Tensor,
        l1: Tensor,
        l2: Tensor,
        y_batch: Optional[Tensor] = None,
        auto_encode: bool = False,
        do_inverse: bool = False,
    ) -> Dict[str, Optional[Tensor]]:
        # prepare y_latent
        if y_batch is None:
            y1 = y2 = y_latent = None
        else:
            y_batch = self.y_fn(y_batch)
            y_latent = self.y_invertible(y_batch)
            if not isinstance(y_latent, tuple):
                y1, y2 = y_latent.chunk(2, dim=1)
            else:
                y1, y2 = y_latent
                y_latent = torch.cat(y_latent, dim=1)
        # simulate cdf
        y_ae = y_inverse = None
        q_inverse_latent = qy_inverse_latent = None
        if y_latent is None:
            q = q_logit = yq_latent = None
        else:
            if auto_encode:
                y_ae = self.y_invertible.inverse(y_latent)
                y_ae = self.y_inv_fn(y_ae)
            y1, y2 = y1 + l1, y2 + l2
            for i in range(self.num_blocks):
                y1, y2 = self.blocks[self.num_blocks - i - 1].inverse(y1, y2)
            yq_latent = torch.cat([y1, y2], dim=1)
            q_logit = self.q_invertible.inverse((y1, y2))
            q = self.q_inv_fn(q_logit)
            with self._detach_q():
                if not do_inverse:
                    q_inverse_latent = self.q_invertible(q.detach())
                else:
                    inverse_results = self.forward(
                        net,
                        l1.detach(),
                        l2.detach(),
                        q_batch=q,
                        do_inverse=False,
                    )
                    y_inverse = inverse_results["y"]
                    q_inverse_latent = inverse_results["q_latent"]
                    qy_inverse_latent = inverse_results["qy_latent"]
        return {
            "q": q,
            "q_logit": q_logit,
            "y_ae": y_ae,
            "y_latent": y_latent,
            "yq_latent": yq_latent,
            "y_inverse": y_inverse,
            "q_inverse_latent": q_inverse_latent,
            "qy_inverse_latent": qy_inverse_latent,
        }

    def forward(
        self,
        net: Tensor,
        l1: Optional[Tensor] = None,
        l2: Optional[Tensor] = None,
        *,
        q_batch: Optional[Tensor] = None,
        y_batch: Optional[Tensor] = None,
        auto_encode: bool = False,
        do_inverse: bool = False,
        median: bool = False,
    ) -> Dict[str, Optional[Tensor]]:
        if l1 is None or l2 is None:
            latent = self.to_latent(net)
            l1, l2 = latent.chunk(2, dim=1)
        assert l1 is not None and l2 is not None
        results: Dict[str, Optional[Tensor]] = {}
        results.update(
            self._get_q_results(net, l1, l2, q_batch, auto_encode, do_inverse, median)
        )
        results.update(
            self._get_y_results(net, l1, l2, y_batch, auto_encode, do_inverse)
        )
        return results


__all__ = ["DDRCore"]
