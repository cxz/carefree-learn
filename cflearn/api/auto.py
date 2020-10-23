import os
import torch
import optuna

import optuna.visualization as vis

from typing import *
from tqdm import tqdm
from functools import partial
from cftool.misc import shallow_copy_dict
from cftool.misc import lock_manager
from cftool.misc import Saving
from cftool.ml.utils import scoring_fn_type
from cfdata.tabular import task_type_type
from cfdata.tabular import parse_task_type
from cfdata.tabular import TabularData
from optuna.trial import TrialState
from optuna.trial import FrozenTrial
from optuna.importance import BaseImportanceEvaluator
from plotly.graph_objects import Figure

from .basic import *
from .ensemble import *
from .hpo import optuna_tune
from .hpo import optuna_params_type
from .hpo import OptunaPresetParams
from .production import Pack
from .production import Predictor
from ..types import data_type


class UnPacked(NamedTuple):
    pattern: EnsemblePattern
    predictors: Dict[str, List[Predictor]]


class Auto:
    data_folder = "__data__"

    def __init__(
        self,
        task_type: task_type_type,
        *,
        models: Union[str, List[str]] = "auto",
        tune_lr: bool = True,
        tune_optimizer: bool = True,
        tune_ema_decay: bool = True,
        tune_clip_norm: bool = True,
        tune_init_method: bool = True,
        **kwargs: Any,
    ):
        self.task_type = task_type
        self.preset_params = OptunaPresetParams(
            tune_lr=tune_lr,
            tune_optimizer=tune_optimizer,
            tune_ema_decay=tune_ema_decay,
            tune_clip_norm=tune_clip_norm,
            tune_init_method=tune_init_method,
            **kwargs,
        )
        # models
        if isinstance(models, list):
            self.models = models
        else:
            if models != "auto":
                self.models = [models]
            else:
                self.models = ["fcnn", "tree_dnn"]
                parsed_task_type = parse_task_type(task_type)
                if parsed_task_type.is_ts:
                    self.models += ["rnn", "transformer"]
                elif parsed_task_type.is_clf:
                    self.models += ["nnb", "ndt"]
                else:
                    self.models.append("ddr")
        if not self.models:
            raise ValueError("`models` should be provided")

    def __str__(self) -> str:
        model_str = ", ".join(self.models)
        return f"Auto({model_str})({self.task_type})"

    __repr__ = __str__

    @property
    def studies(self) -> Dict[str, optuna.study.Study]:
        return {k: v.study for k, v in self.optuna_results.items()}

    @property
    def predict(self) -> Callable:
        return self.pattern.predict

    @property
    def predict_prob(self) -> Callable:
        return partial(self.pattern.predict, requires_prob=True)

    @property
    def pruned_trials(self) -> Dict[str, List[FrozenTrial]]:
        return {
            k: [t for t in v.trials if t.state == TrialState.PRUNED]
            for k, v in self.studies.items()
        }

    @property
    def complete_trials(self) -> Dict[str, List[FrozenTrial]]:
        return {
            k: [t for t in v.trials if t.state == TrialState.COMPLETE]
            for k, v in self.studies.items()
        }

    # api

    def fit(
        self,
        x: data_type,
        y: data_type = None,
        x_cv: data_type = None,
        y_cv: data_type = None,
        *,
        study_config: Optional[Dict[str, Any]] = None,
        predict_config: Optional[Dict[str, Any]] = None,
        metrics: Optional[Union[str, List[str]]] = None,
        params: Optional[Dict[str, optuna_params_type]] = None,
        num_jobs: int = 1,
        num_trial: int = 50,
        num_repeat: int = 3,
        num_parallel: int = 0,
        timeout: Optional[float] = None,
        score_weights: Optional[Dict[str, float]] = None,
        estimator_scoring_function: Union[str, scoring_fn_type] = "mean",
        temp_folder: str = "__tmp__",
        num_final_repeat: int = 20,
        extra_config: Optional[Dict[str, Any]] = None,
        cuda: Optional[Union[str, int]] = None,
    ) -> "Auto":
        self.best_params = {}
        self.optuna_results = {}

        tuner = None
        optuna_folder = os.path.join(temp_folder, "__optuna__")
        for model in self.models:
            if params is not None:
                model_params = params[model]
            else:
                model_params = self.preset_params.get(model)

            args = () if tuner is not None else (x, y, x_cv, y_cv)
            optuna_result = optuna_tune(
                *args,
                model=model,
                task_type=self.task_type,
                tuner=tuner,
                params=model_params,
                study_config=study_config,
                metrics=metrics,
                num_jobs=num_jobs,
                num_trial=num_trial,
                num_repeat=num_repeat,
                num_parallel=num_parallel,
                timeout=timeout,
                score_weights=score_weights,
                estimator_scoring_function=estimator_scoring_function,
                temp_folder=optuna_folder,
                extra_config=extra_config,
                cuda=cuda,
            )
            tuner = optuna_result.tuner
            best_param = optuna_result.best_param
            self.best_params[model] = best_param
            self.optuna_results[model] = optuna_result

        num_jobs = max(num_jobs, num_parallel)
        repeat_temp_folder = os.path.join(temp_folder, "__repeat__")
        os.makedirs(repeat_temp_folder, exist_ok=True)
        model_configs = shallow_copy_dict(self.best_params)
        repeat_config = {
            "models": self.models,
            "model_configs": model_configs,
            "sequential": num_jobs <= 1,
            "predict_config": predict_config,
            "temp_folder": repeat_temp_folder,
            "num_repeat": num_final_repeat,
            "num_jobs": num_jobs,
        }
        assert tuner is not None
        x, y, x_cv, y_cv = tuner.make_data()
        self.repeat_result = repeat_with(x, y, x_cv, y_cv, **repeat_config)
        self.pipelines = self.repeat_result.pipelines
        self.patterns = self.repeat_result.patterns
        self.data = self.repeat_result.data
        assert self.pipelines is not None
        assert self.patterns is not None

        all_patterns = []
        for v in self.patterns.values():
            all_patterns.extend(v)
        self.pattern = ensemble(all_patterns)

        return self

    def pack(
        self,
        export_folder: str,
        *,
        verbose: bool = False,
        use_tqdm: bool = True,
        compress: bool = True,
        retain_data: bool = False,
        remove_original: bool = True,
    ) -> "Auto":
        if self.pipelines is None:
            raise ValueError("`pipelines` are not yet generated")
        abs_folder = os.path.abspath(export_folder)
        base_folder = os.path.dirname(abs_folder)
        with lock_manager(base_folder, [export_folder]):
            Saving.prepare_folder(self, export_folder)
            data_folder = os.path.join(export_folder, self.data_folder)
            if self.data is None:
                raise ValueError("`data` is not generated yet")
            self.data.save(
                data_folder,
                retain_data=retain_data,
                compress=False,
            )
            iterator = self.models
            if use_tqdm:
                iterator = tqdm(iterator, "pack")
            for model in iterator:
                pipelines = self.pipelines[model]
                model_folder = os.path.join(export_folder, model)
                for i, pipeline in enumerate(pipelines):
                    local_export_folder = os.path.join(model_folder, f"m_{i:04d}")
                    Pack.pack(
                        pipeline,
                        local_export_folder,
                        verbose=verbose,
                        pack_data=False,
                        compress=False,
                    )
            if compress:
                Saving.compress(abs_folder, remove_original=remove_original)
        return self

    @classmethod
    def unpack(
        cls,
        export_folder: str,
        device: Union[str, torch.device] = "cpu",
        *,
        compress: bool = True,
        use_tqdm: bool = True,
        use_tqdm_in_predictor: bool = False,
        **predict_kwargs: Any,
    ) -> UnPacked:
        patterns = []
        base_folder = os.path.dirname(os.path.abspath(export_folder))
        with lock_manager(base_folder, [export_folder]):
            with Saving.compress_loader(
                export_folder,
                compress,
                remove_extracted=True,
            ):
                data_folder = os.path.join(export_folder, cls.data_folder)
                data = TabularData.load(data_folder, compress=False)
                predictors = {}
                iterator = [
                    folder
                    for folder in os.listdir(export_folder)
                    if folder != cls.data_folder
                ]
                if use_tqdm:
                    iterator = tqdm(iterator)
                for model in iterator:
                    local_predictors = []
                    model_folder = os.path.join(export_folder, model)
                    for sub_folder in os.listdir(model_folder):
                        sub_folder = os.path.join(model_folder, sub_folder)
                        local_predictor = Pack.get_predictor(
                            sub_folder,
                            device,
                            data=data,
                            compress=False,
                            use_tqdm=use_tqdm_in_predictor,
                        )
                        local_predictors.append(local_predictor)
                        patterns.append(local_predictor.to_pattern(**predict_kwargs))
                    predictors[model] = local_predictors
        pattern = ensemble(patterns)
        return UnPacked(pattern, predictors)

    # visualization

    def plot_param_importances(
        self,
        model: str,
        evaluator: Optional[BaseImportanceEvaluator] = None,
        params: Optional[List[str]] = None,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_param_importances(self.studies[model], evaluator, params)
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "param_importances.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_contour(
        self,
        model: str,
        params: Optional[List[str]] = None,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_contour(self.studies[model], params)
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "contour.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_parallel_coordinate(
        self,
        model: str,
        params: Optional[List[str]] = None,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_parallel_coordinate(self.studies[model], params)
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "parallel_coordinate.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_slice(
        self,
        model: str,
        params: Optional[List[str]] = None,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_slice(self.studies[model], params)
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "slice.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_optimization_history(
        self,
        model: str,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_optimization_history(self.studies[model])
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "optimization_history.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_intermediate_values(
        self,
        model: str,
        export_folder: Optional[str] = None,
    ) -> Figure:
        fig = vis.plot_intermediate_values(self.studies[model])
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "intermediate_values.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig

    def plot_edf(self, model: str, export_folder: str = None) -> Figure:
        fig = vis.plot_edf(self.studies[model])
        if export_folder is not None:
            os.makedirs(export_folder, exist_ok=True)
            html_path = os.path.join(export_folder, "edf.html")
            with open(html_path, "w") as f:
                f.write(fig.to_html())
        return fig


__all__ = ["Auto"]
