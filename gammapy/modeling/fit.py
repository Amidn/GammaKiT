# Licensed under a 3-clause BSD style license - see LICENSE.rst
import html
import itertools
import logging
import numpy as np
from astropy.table import Table
from gammapy.utils.pbar import progress_bar
from gammapy.modeling.utils import _parse_datasets
from .covariance import Covariance
from .iminuit import (
    confidence_iminuit,
    contour_iminuit,
    covariance_iminuit,
    optimize_iminuit,
)
from .scipy import confidence_scipy, optimize_scipy
from .sherpa import optimize_sherpa

__all__ = ["Fit", "FitResult", "OptimizeResult", "CovarianceResult"]

log = logging.getLogger(__name__)


class Registry:
    """Registry of available backends for given tasks.

    Gives users the power to extend from their scripts.
    Used by `Fit` below.

    Not sure if we should call it "backend" or "method" or something else.
    Probably we will code up some methods, e.g. for profile analysis ourselves,
    using scipy or even just Python / Numpy?
    """

    register = {
        "optimize": {
            "minuit": optimize_iminuit,
            "sherpa": optimize_sherpa,
            "scipy": optimize_scipy,
        },
        "covariance": {
            "minuit": covariance_iminuit,
            # "sherpa": covariance_sherpa,
            # "scipy": covariance_scipy,
        },
        "confidence": {
            "minuit": confidence_iminuit,
            # "sherpa": confidence_sherpa,
            "scipy": confidence_scipy,
        },
    }

    @classmethod
    def get(cls, task, backend):
        if task not in cls.register:
            raise ValueError(f"Unknown task {task!r}")

        backend_options = cls.register[task]

        if backend not in backend_options:
            raise ValueError(f"Unknown backend {backend!r} for task {task!r}")

        return backend_options[backend]


registry = Registry()


class Fit:
    """Fit class.

    The fit class provides a uniform interface to multiple fitting backends.
    Currently available: "minuit", "sherpa" and "scipy".

    Parameters
    ----------
    backend : {"minuit", "scipy" "sherpa"}
        Global backend used for fitting. Default is "minuit".
    optimize_opts : dict
        Keyword arguments passed to the optimizer. For the `"minuit"` backend
        see https://iminuit.readthedocs.io/en/stable/reference.html#iminuit.Minuit
        for a detailed description of the available options. If there is an entry
        'migrad_opts', those options will be passed to `iminuit.Minuit.migrad()`.

        For the `"sherpa"` backend you can from the options:

            * `"simplex"`
            * `"levmar"`
            * `"moncar"`
            * `"gridsearch"`

        Those methods are described and compared in detail on
        http://cxc.cfa.harvard.edu/sherpa/methods/index.html. The available
        options of the optimization methods are described on the following
        pages in detail:

            * http://cxc.cfa.harvard.edu/sherpa/ahelp/neldermead.html
            * http://cxc.cfa.harvard.edu/sherpa/ahelp/montecarlo.html
            * http://cxc.cfa.harvard.edu/sherpa/ahelp/gridsearch.html
            * http://cxc.cfa.harvard.edu/sherpa/ahelp/levmar.html

        For the `"scipy"` backend the available options are described in detail here:
        https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.minimize.html

    covariance_opts : dict
        Covariance options passed to the given backend.
    confidence_opts : dict
        Extra arguments passed to the backend. E.g. `iminuit.Minuit.minos` supports
        a ``maxcall`` option. For the scipy backend ``confidence_opts`` are forwarded
        to `~scipy.optimize.brentq`. If the confidence estimation fails, the bracketing
        interval can be adapted by modifying the upper bound of the interval (``b``) value.
    store_trace : bool
        Whether to store the trace of the fit.
    """

    def __init__(
        self,
        backend="minuit",
        optimize_opts=None,
        covariance_opts=None,
        confidence_opts=None,
        store_trace=False,
    ):
        self.store_trace = store_trace
        self.backend = backend

        if optimize_opts is None:
            optimize_opts = {"backend": backend}

        if covariance_opts is None:
            covariance_opts = {"backend": backend}

        if confidence_opts is None:
            confidence_opts = {"backend": backend}

        self.optimize_opts = optimize_opts
        self.covariance_opts = covariance_opts
        self.confidence_opts = confidence_opts
        self._minuit = None

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"

    def run(self, datasets):
        """Run all fitting steps.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.

        Returns
        -------
        fit_result : `FitResult`
            Fit result.
        """

        datasets, parameters = _parse_datasets(datasets=datasets)

        optimize_result = self.optimize(datasets=datasets)

        if self.backend not in registry.register["covariance"]:
            log.warning("No covariance estimate - not supported by this backend.")
            return FitResult(optimize_result=optimize_result)

        covariance_result = self.covariance(
            datasets=datasets, optimize_result=optimize_result
        )

        optimize_result.models.covariance = Covariance(
            optimize_result.models.parameters, covariance_result.matrix
        )

        datasets._covariance = Covariance(parameters, covariance_result.matrix)

        return FitResult(
            optimize_result=optimize_result,
            covariance_result=covariance_result,
        )

    def optimize(self, datasets):
        """Run the optimization.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.

        Returns
        -------
        optimize_result : `OptimizeResult`
            Optimization result.
        """
        datasets, parameters = _parse_datasets(datasets=datasets)
        datasets.parameters.check_limits()

        if len(parameters.free_parameters.names) == 0:
            raise ValueError("No free parameters for fitting")

        parameters.autoscale()

        kwargs = self.optimize_opts.copy()
        backend = kwargs.pop("backend", self.backend)

        compute = registry.get("optimize", backend)
        # TODO: change this calling interface!
        # probably should pass a fit statistic, which has a model, which has parameters
        # and return something simpler, not a tuple of three things
        factors, info, optimizer = compute(
            parameters=parameters,
            function=datasets.stat_sum,
            store_trace=self.store_trace,
            **kwargs,
        )

        if backend == "minuit":
            self._minuit = optimizer
            kwargs["method"] = "migrad"

        trace = Table(info.pop("trace"))

        if self.store_trace:
            idx = [
                parameters.index(par)
                for par in parameters.unique_parameters.free_parameters
            ]
            unique_names = np.array(datasets.models.parameters_unique_names)[idx]
            trace.rename_columns(trace.colnames[1:], list(unique_names))

        # Copy final results into the parameters object
        parameters.set_parameter_factors(factors)
        parameters.check_limits()

        return OptimizeResult(
            models=datasets.models.copy(),
            total_stat=datasets.stat_sum(),
            backend=backend,
            method=kwargs.get("method", backend),
            trace=trace,
            minuit=optimizer,
            **info,
        )

    def covariance(self, datasets, optimize_result=None):
        """Estimate the covariance matrix.

        Assumes that the model parameters are already optimised.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.
        optimize_result : `OptimizeResult`, optional
            Optimization result. Can be optionally used to pass the state of the IMinuit object
            to the covariance estimation. This might save computation time in certain cases.
            Default is None.

        Returns
        -------
        result : `CovarianceResult`
            Results.
        """
        datasets, unique_pars = _parse_datasets(datasets=datasets)
        parameters = datasets.models.parameters

        kwargs = self.covariance_opts.copy()

        if optimize_result is not None and optimize_result.backend == "minuit":
            kwargs["minuit"] = optimize_result.minuit

        backend = kwargs.pop("backend", self.backend)
        compute = registry.get("covariance", backend)

        with unique_pars.restore_status():
            if self.backend == "minuit":
                method = "hesse"
            else:
                method = ""

            factor_matrix, info = compute(
                parameters=unique_pars, function=datasets.stat_sum, **kwargs
            )

            matrix = Covariance.from_factor_matrix(
                parameters=parameters, matrix=factor_matrix
            )
            datasets.models.covariance = matrix

        if optimize_result:
            optimize_result.models.covariance = matrix.data.copy()

        return CovarianceResult(
            backend=backend,
            method=method,
            success=info["success"],
            message=info["message"],
            matrix=matrix.data,
        )

    def confidence(self, datasets, parameter, sigma=1, reoptimize=True):
        """Estimate confidence interval.

        Extra ``kwargs`` are passed to the backend.
        E.g. `iminuit.Minuit.minos` supports a ``maxcall`` option.

        For the scipy backend ``kwargs`` are forwarded to `~scipy.optimize.brentq`. If the
        confidence estimation fails, the bracketing interval can be adapted by modifying the
        upper bound of the interval (``b``) value.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.
        parameter : `~gammapy.modeling.Parameter`
            Parameter of interest.
        sigma : float, optional
            Number of standard deviations for the confidence level. Default is 1.
        reoptimize : bool, optional
            Re-optimize other parameters, when computing the confidence region.
            Default is True.

        Returns
        -------
        result : dict
            Dictionary with keys "errp", 'errn", "success" and "nfev".
        """
        datasets, parameters = _parse_datasets(datasets=datasets)

        kwargs = self.confidence_opts.copy()
        backend = kwargs.pop("backend", self.backend)

        compute = registry.get("confidence", backend)
        parameter = parameters[parameter]

        with parameters.restore_status():
            result = compute(
                parameters=parameters,
                parameter=parameter,
                function=datasets.stat_sum,
                sigma=sigma,
                reoptimize=reoptimize,
                **kwargs,
            )

        result["errp"] *= parameter.scale
        result["errn"] *= parameter.scale
        return result

    def stat_profile(self, datasets, parameter, reoptimize=False):
        """Compute fit statistic profile.

        The method used is to vary one parameter, keeping all others fixed.
        So this is taking a "slice" or "scan" of the fit statistic.

        Notes
        -----
        The progress bar can be displayed for this function.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.
        parameter : `~gammapy.modeling.Parameter`
            Parameter of interest. The specification for the scan, such as bounds
            and number of values is taken from the parameter object.
        reoptimize : bool, optional
            Re-optimize other parameters, when computing the confidence region. Default is False.

        Returns
        -------
        results : dict
            Dictionary with keys "parameter_name_scan", "stat_scan" and "fit_results". The latter contains an
            empty list, if `reoptimize` is set to False.

        Examples
        --------
        >>> from gammapy.datasets import Datasets, SpectrumDatasetOnOff
        >>> from gammapy.modeling.models import SkyModel, LogParabolaSpectralModel
        >>> from gammapy.modeling import Fit
        >>> datasets = Datasets()
        >>> for obs_id in [23523, 23526]:
        ...     dataset = SpectrumDatasetOnOff.read(
        ...         f"$GAMMAPY_DATA/joint-crab/spectra/hess/pha_obs{obs_id}.fits"
        ...     )
        ...     datasets.append(dataset)
        >>> datasets = datasets.stack_reduce(name="HESS")
        >>> model = SkyModel(spectral_model=LogParabolaSpectralModel(), name="crab")
        >>> datasets.models = model
        >>> fit = Fit()
        >>> result = fit.run(datasets)
        >>> parameter = datasets.models.parameters['amplitude']
        >>> stat_profile = fit.stat_profile(datasets=datasets, parameter=parameter)
        """
        datasets, parameters = _parse_datasets(datasets=datasets)

        parameter = parameters[parameter]
        values = parameter.scan_values

        stats = []
        fit_results = []
        with parameters.restore_status():
            for value in progress_bar(values, desc="Scan values"):
                parameter.value = value
                if reoptimize:
                    parameter.frozen = True
                    result = self.optimize(datasets=datasets)
                    stat = result.total_stat
                    fit_results.append(result)
                else:
                    stat = datasets.stat_sum()
                stats.append(stat)

        idx = datasets.parameters.index(parameter)
        name = datasets.models.parameters_unique_names[idx]

        return {
            f"{name}_scan": values,
            "stat_scan": np.array(stats),
            "fit_results": fit_results,
        }

    def stat_surface(self, datasets, x, y, reoptimize=False):
        """Compute fit statistic surface.

        The method used is to vary two parameters, keeping all others fixed.
        So this is taking a "slice" or "scan" of the fit statistic.

        Caveat: This method can be very computationally intensive and slow

        See also: `Fit.stat_contour`.

        Notes
        -----
        The progress bar can be displayed for this function.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.
        x, y : `~gammapy.modeling.Parameter`
            Parameters of interest.
        reoptimize : bool, optional
            Re-optimize other parameters, when computing the confidence region. Default is False.

        Returns
        -------
        results : dict
            Dictionary with keys "x_values", "y_values", "stat" and "fit_results".
            The latter contains an empty list, if `reoptimize` is set to False.

        Examples
        --------
        >>> from gammapy.datasets import Datasets, SpectrumDatasetOnOff
        >>> from gammapy.modeling.models import SkyModel, LogParabolaSpectralModel
        >>> from gammapy.modeling import Fit
        >>> import numpy as np
        >>> datasets = Datasets()
        >>> for obs_id in [23523, 23526]:
        ...     dataset = SpectrumDatasetOnOff.read(
        ...         f"$GAMMAPY_DATA/joint-crab/spectra/hess/pha_obs{obs_id}.fits"
        ...     )
        ...     datasets.append(dataset)
        >>> datasets = datasets.stack_reduce(name="HESS")
        >>> model = SkyModel(spectral_model=LogParabolaSpectralModel(), name="crab")
        >>> datasets.models = model
        >>> par_alpha = datasets.models.parameters["alpha"]
        >>> par_beta = datasets.models.parameters["beta"]
        >>> par_alpha.scan_values = np.linspace(1.55, 2.7, 20)
        >>> par_beta.scan_values = np.linspace(-0.05, 0.55, 20)
        >>> fit = Fit()
        >>> stat_surface = fit.stat_surface(
        ...     datasets=datasets,
        ...     x=par_alpha,
        ...     y=par_beta,
        ...     reoptimize=False,
        ... )
        """
        datasets, parameters = _parse_datasets(datasets=datasets)

        x = parameters[x]
        y = parameters[y]

        stats = []
        fit_results = []

        with parameters.restore_status():
            for x_value, y_value in progress_bar(
                itertools.product(x.scan_values, y.scan_values), desc="Trial values"
            ):
                x.value, y.value = x_value, y_value

                if reoptimize:
                    x.frozen, y.frozen = True, True
                    result = self.optimize(datasets=datasets)
                    stat = result.total_stat
                    fit_results.append(result)
                else:
                    stat = datasets.stat_sum()

                stats.append(stat)

        shape = (len(x.scan_values), len(y.scan_values))
        stats = np.array(stats).reshape(shape)

        if reoptimize:
            fit_results = np.array(fit_results).reshape(shape)

        i1, i2 = datasets.parameters.index(x), datasets.parameters.index(y)
        name_x = datasets.models.parameters_unique_names[i1]
        name_y = datasets.models.parameters_unique_names[i2]

        return {
            f"{name_x}_scan": x.scan_values,
            f"{name_y}_scan": y.scan_values,
            "stat_scan": stats,
            "fit_results": fit_results,
        }

    def stat_contour(self, datasets, x, y, numpoints=10, sigma=1):
        """Compute stat contour.

        Calls ``iminuit.Minuit.mncontour``.

        This is a contouring algorithm for a 2D function
        which is not simply the fit statistic function.
        That 2D function is given at each point ``(par_1, par_2)``
        by re-optimising all other free parameters,
        and taking the fit statistic at that point.

        Very compute-intensive and slow.

        Parameters
        ----------
        datasets : `Datasets` or list of `Dataset`
            Datasets to optimize.
        x, y : `~gammapy.modeling.Parameter`
            Parameters of interest.
        numpoints : int, optional
            Number of contour points. Default is 10.
        sigma : float, optional
            Number of standard deviations for the confidence level. Default is 1.

        Returns
        -------
        result : dict
            Dictionary containing the parameter values defining the contour, with the
            boolean flag "success" and the information objects from ``mncontour``.

        Examples
        --------
        >>> from gammapy.datasets import Datasets, SpectrumDatasetOnOff
        >>> from gammapy.modeling.models import SkyModel, LogParabolaSpectralModel
        >>> from gammapy.modeling import Fit
        >>> datasets = Datasets()
        >>> for obs_id in [23523, 23526]:
        ...     dataset = SpectrumDatasetOnOff.read(
        ...         f"$GAMMAPY_DATA/joint-crab/spectra/hess/pha_obs{obs_id}.fits"
        ...     )
        ...     datasets.append(dataset)
        >>> datasets = datasets.stack_reduce(name="HESS")
        >>> model = SkyModel(spectral_model=LogParabolaSpectralModel(), name="crab")
        >>> datasets.models = model
        >>> fit = Fit(backend='minuit')
        >>> optimize = fit.optimize(datasets)
        >>> stat_contour = fit.stat_contour(
        ...     datasets=datasets,
        ...     x=model.spectral_model.alpha,
        ...     y=model.spectral_model.amplitude,
        ... )
        """

        datasets, parameters = _parse_datasets(datasets=datasets)

        x = parameters[x]
        y = parameters[y]

        i1, i2 = datasets.parameters.index(x), datasets.parameters.index(y)
        name_x = datasets.models.parameters_unique_names[i1]
        name_y = datasets.models.parameters_unique_names[i2]

        with parameters.restore_status():
            result = contour_iminuit(
                parameters=parameters,
                function=datasets.stat_sum,
                x=x,
                y=y,
                numpoints=numpoints,
                sigma=sigma,
            )

        x = result["x"] * x.scale
        y = result["y"] * y.scale

        return {
            name_x: x,
            name_y: y,
            "success": result["success"],
        }


class FitStepResult:
    """Fit result base class."""

    def __init__(self, backend, method, success, message):
        self._success = success
        self._message = message
        self._backend = backend
        self._method = method

    @property
    def backend(self):
        """Optimizer backend used for the fit."""
        return self._backend

    @property
    def method(self):
        """Optimizer method used for the fit."""
        return self._method

    @property
    def success(self):
        """Fit success status flag."""
        return self._success

    @property
    def message(self):
        """Optimizer status message."""
        return self._message

    def __str__(self):
        return (
            f"{self.__class__.__name__}\n\n"
            f"\tbackend    : {self.backend}\n"
            f"\tmethod     : {self.method}\n"
            f"\tsuccess    : {self.success}\n"
            f"\tmessage    : {self.message}\n"
        )

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"

    def to_dict(self):
        """Convert to dictionary."""
        return {
            self.__class__.__name__: {
                "backend": self.backend,
                "method": self.method,
                "success": self.success,
                "message": self.message,
            }
        }


class CovarianceResult(FitStepResult):
    """Covariance result object.

    Parameters
    ----------
    matrix : `~numpy.ndarray`, optional
        The covariance matrix. Default is None.
    kwargs : dict
        Extra ``kwargs`` are passed to the backend.
    """

    def __init__(self, matrix=None, **kwargs):
        self._matrix = matrix
        super().__init__(**kwargs)

    @property
    def matrix(self):
        """Covariance matrix as a `~numpy.ndarray`."""
        return self._matrix


class OptimizeResult(FitStepResult):
    """Optimize result object.

    Parameters
    ----------
    models : `~gammapy.modeling.models.DatasetModels`
        Best fit models.
    nfev : int
        Number of function evaluations.
    total_stat : float
        Value of the fit statistic at minimum.
    trace : `~astropy.table.Table`
        Parameter trace from the optimisation.
    minuit : `~iminuit.minuit.Minuit`, optional
        Minuit object. Default is None.
    kwargs : dict
        Extra ``kwargs`` are passed to the backend.
    """

    def __init__(self, models, nfev, total_stat, trace, minuit=None, **kwargs):
        self._models = models
        self._nfev = nfev
        self._total_stat = total_stat
        self._trace = trace
        self._minuit = minuit
        super().__init__(**kwargs)

    @property
    def minuit(self):
        """Minuit object."""
        return self._minuit

    @property
    def parameters(self):
        """Best fit parameters."""
        return self.models.parameters

    @property
    def models(self):
        """Best fit models."""
        return self._models

    @property
    def trace(self):
        """Parameter trace from the optimisation."""
        return self._trace

    @property
    def nfev(self):
        """Number of function evaluations."""
        return self._nfev

    @property
    def total_stat(self):
        """Value of the fit statistic at minimum."""
        return self._total_stat

    def __str__(self):
        string = super().__str__()
        string += f"\tnfev       : {self.nfev}\n"
        string += f"\ttotal stat : {self.total_stat:.2f}\n\n"
        return string

    def to_dict(self):
        """Convert to dictionary."""
        output = super().to_dict()
        output[self.__class__.__name__]["nfev"] = self.nfev
        output[self.__class__.__name__]["total_stat"] = float(self._total_stat)
        return output


class FitResult:
    """Fit result class.

    The fit result class provides the results from the optimisation and covariance of the fit.

    Parameters
    ----------
    optimize_result : `~OptimizeResult`
        Result of the optimization step.
    covariance_result : `~CovarianceResult`
        Result of the covariance step.
    """

    def __init__(self, optimize_result=None, covariance_result=None):
        self._optimize_result = optimize_result
        self._covariance_result = covariance_result

    @property
    def minuit(self):
        """Minuit object."""
        return self.optimize_result.minuit

    @property
    def parameters(self):
        """Best fit parameters of the optimization step."""
        return self.optimize_result.parameters

    @property
    def models(self):
        """Best fit parameters of the optimization step."""
        return self.optimize_result.models

    @property
    def total_stat(self):
        """Total stat of the optimization step."""
        return self.optimize_result.total_stat

    @property
    def trace(self):
        """Parameter trace of the optimisation step."""
        return self.optimize_result.trace

    @property
    def nfev(self):
        """Number of function evaluations of the optimisation step."""
        return self.optimize_result.nfev

    @property
    def backend(self):
        """Optimizer backend used for the fit."""
        return self.optimize_result.backend

    @property
    def method(self):
        """Optimizer method used for the fit."""
        return self.optimize_result.method

    @property
    def message(self):
        """Optimizer status message."""
        return self.optimize_result.message

    @property
    def success(self):
        """Total success flag."""
        success = self.optimize_result.success

        if self.covariance_result:
            success &= self.covariance_result.success

        return success

    @property
    def optimize_result(self):
        """Optimize result."""
        return self._optimize_result

    @property
    def covariance_result(self):
        """Optimize result."""
        return self._covariance_result

    def write(
        self,
        path,
        overwrite=False,
        full_output=True,
        overwrite_templates=False,
        write_covariance=True,
        checksum=False,
    ):
        """Write to file.

        Parameters
        ----------
        path : `pathlib.Path` or str
            Path to write files.
        overwrite : bool, optional
            Overwrite existing file. Default is False.
        full_output : bool, optional
            Store full parameter output. Default is True.
        overwrite_templates : bool, optional
            Overwrite templates FITS files. Default is False.
        checksum : bool, optional
            When True adds a CHECKSUM entry to the file.
            Default is False.
        """
        from gammapy.modeling.models.core import _write_models

        output = {}
        if self.optimize_result is not None:
            output.update(self.optimize_result.to_dict())
        if self.covariance_result is not None:
            output.update(self.covariance_result.to_dict())
        _write_models(
            self.models,
            path,
            overwrite,
            full_output,
            overwrite_templates,
            write_covariance,
            extra_dict=output,
        )

    def __str__(self):
        string = ""
        if self.optimize_result:
            string += str(self.optimize_result)

        if self.covariance_result:
            string += str(self.covariance_result)

        return string

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"
