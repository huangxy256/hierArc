from hierarc.Likelihood.transformed_cosmography import TransformedCosmography
from hierarc.Likelihood.LensLikelihood.lens_likelihood import LensLikelihoodBase
from hierarc.Likelihood.anisotropy_scaling import AnisotropyScalingIFU
from hierarc.Util.distribution_util import PDFSampling
import numpy as np


class LensLikelihood(TransformedCosmography, LensLikelihoodBase, AnisotropyScalingIFU):
    """
    master class containing the likelihood definitions of different analysis
    """
    def __init__(self, z_lens, z_source, name='name', likelihood_type='TDKin', anisotropy_model='NONE',
                 ani_param_array=None, ani_scaling_array_list=None, ani_scaling_array=None,
                 num_distribution_draws=50, kappa_ext_bias=False, kappa_pdf=None, kappa_bin_edges=None, mst_ifu=False,
                 **kwargs_likelihood):
        """

        :param z_lens: lens redshift
        :param z_source: source redshift
        :param name: string (optional) to name the specific lens
        :param likelihood_type: string to specify the likelihood type
        :param ani_param_array: array of anisotropy parameter values for which the kinematics are predicted
        :param ani_scaling_array: velocity dispersion sigma**2 scaling of anisotropy parameter relative to default prediction
        :param ani_scaling_array_list: list of array with the scalings of J() for each IFU
        :param num_distribution_draws: int, number of distribution draws from the likelihood that are being averaged over
        :param kappa_ext_bias: bool, if True incorporates the global external selection function into the likelihood.
        If False, the likelihood needs to incorporate the individual selection function with sufficient accuracy.
        :param kappa_pdf: array of probability density function of the external convergence distribution
         binned according to kappa_bin_edges
        :param kappa_bin_edges: array of length (len(kappa_pdf)+1), bin edges of the kappa PDF
        :param mst_ifu: bool, if True replaces the lambda_mst parameter by the lambda_ifu parameter (and distribution)
         in sampling this lens.
        :param kwargs_likelihood: keyword arguments specifying the likelihood function,
        see individual classes for their use
        """
        TransformedCosmography.__init__(self, z_lens=z_lens, z_source=z_source)
        if ani_scaling_array_list is None and ani_scaling_array is not None:
            ani_scaling_array_list = [ani_scaling_array]
        AnisotropyScalingIFU.__init__(self, anisotropy_model=anisotropy_model, ani_param_array=ani_param_array,
                                      ani_scaling_array_list=ani_scaling_array_list)
        LensLikelihoodBase.__init__(self, z_lens=z_lens, z_source=z_source, likelihood_type=likelihood_type, name=name,
                                    **kwargs_likelihood)
        self._num_distribution_draws = int(num_distribution_draws)
        self._kappa_ext_bias = kappa_ext_bias
        self._mst_ifu = mst_ifu
        if kappa_pdf is not None and kappa_bin_edges is not None:
            self._kappa_dist = PDFSampling(bin_edges=kappa_bin_edges, pdf_array=kappa_pdf)
            self._draw_kappa = True
        else:
            self._draw_kappa = False

    def lens_log_likelihood(self, cosmo, kwargs_lens=None, kwargs_kin=None):
        """

        :param cosmo: astropy.cosmology instance
        :param kwargs_lens: keywords of the hyper parameters of the lens model
        :param kwargs_kin: keyword arguments of the kinematic model hyper parameters
        :return: log likelihood of the data given the model
        """

        # here we compute the unperturbed angular diameter distances of the lens system given the cosmology
        # Note: Distances are in physical units of Mpc. Make sure the posteriors to evaluate this likelihood is in the
        # same units
        ddt, dd = self.angular_diameter_distances(cosmo)
        # here we effectively change the posteriors of the lens, but rather than changing the instance of the KDE we
        # displace the predicted angular diameter distances in the opposite direction
        return self.hyper_param_likelihood(ddt, dd, kwargs_lens=kwargs_lens, kwargs_kin=kwargs_kin)

    def hyper_param_likelihood(self, ddt, dd, kwargs_lens, kwargs_kin):
        """

        :param ddt: time-delay distance
        :param dd: angular diameter distance to the deflector
        :param kwargs_lens: keywords of the hyper parameters of the lens model
        :param kwargs_kin: keyword arguments of the kinematic model hyper parameters
        :return: log likelihood given the single lens analysis for the given hyper parameter
        """
        sigma_v_sys_error = kwargs_kin.pop('sigma_v_sys_error', None)

        if self.check_dist(kwargs_lens, kwargs_kin):  # sharp distributions
            lambda_mst, kappa_ext, gamma_ppn = self.draw_lens(**kwargs_lens)
            ddt_, dd_ = self.displace_prediction(ddt, dd, gamma_ppn=gamma_ppn, lambda_mst=lambda_mst,
                                                 kappa_ext=kappa_ext)
            aniso_param_array = self.draw_anisotropy(**kwargs_kin)
            aniso_scaling = self.ani_scaling(aniso_param_array)
            lnlog = self.log_likelihood(ddt_, dd_, aniso_scaling=aniso_scaling, sigma_v_sys_error=sigma_v_sys_error)
            return lnlog
        else:
            likelihood = 0
            for i in range(self._num_distribution_draws):
                lambda_mst_draw, kappa_ext_draw, gamma_ppn = self.draw_lens(**kwargs_lens)
                aniso_param_draw = self.draw_anisotropy(**kwargs_kin)
                aniso_scaling = self.ani_scaling(aniso_param_draw)
                ddt_, dd_ = self.displace_prediction(ddt, dd, gamma_ppn=gamma_ppn,
                                                     lambda_mst=lambda_mst_draw,
                                                     kappa_ext=kappa_ext_draw)
                logl = self.log_likelihood(ddt_, dd_, aniso_scaling=aniso_scaling, sigma_v_sys_error=sigma_v_sys_error)
                exp_logl = np.exp(logl)
                if np.isfinite(exp_logl) and exp_logl > 0:
                    likelihood += exp_logl
            if likelihood <= 0:
                return -np.inf
            return np.log(likelihood/self._num_distribution_draws)

    def angular_diameter_distances(self, cosmo):
        """

        :param cosmo: astropy.cosmology instance (or equivalent with interpolation
        :return: ddt, dd in units Mpc
        """
        dd = cosmo.angular_diameter_distance(z=self._z_lens).value
        ds = cosmo.angular_diameter_distance(z=self._z_source).value
        dds = cosmo.angular_diameter_distance_z1z2(z1=self._z_lens, z2=self._z_source).value
        ddt = (1. + self._z_lens) * dd * ds / dds
        return ddt, dd

    def check_dist(self, kwargs_lens, kwargs_kin):
        """
        checks if the provided keyword arguments describe a distribution function of hyper parameters or are single
        values

        :param kwargs_lens: lens model hyper parameter keywords
        :param kwargs_kin: kinematic model hyper parameter keywords
        :return: bool, True if delta function, else False
        """
        lambda_mst_sigma = kwargs_lens.get('lambda_mst_sigma', 0)  # scatter in MST
        kappa_ext_sigma = kwargs_lens.get('kappa_ext_sigma', 0)
        a_ani_sigma = kwargs_kin.get('a_ani_sigma', 0)
        beta_inf_sigma = kwargs_kin.get('beta_inf_sigma', 0)
        if a_ani_sigma == 0 and lambda_mst_sigma == 0 and kappa_ext_sigma == 0 and beta_inf_sigma == 0:
            if self._draw_kappa is False:
                return True
        return False

    def draw_lens(self, lambda_mst=1, lambda_mst_sigma=0, kappa_ext=0, kappa_ext_sigma=0, gamma_ppn=1, lambda_ifu=1,
                  lambda_ifu_sigma=0):
        """

        :param lambda_mst: MST transform
        :param lambda_mst_sigma: spread in the distribution
        :param kappa_ext: external convergence mean in distribution
        :param kappa_ext_sigma: spread in the distribution
        :param gamma_ppn: Post-Newtonian parameter
        :return: draw from the distributions
        """
        if self._mst_ifu is True:
            lambda_mst_draw = np.random.normal(lambda_ifu, lambda_ifu_sigma)
        else:
            lambda_mst_draw = np.random.normal(lambda_mst, lambda_mst_sigma)
        if self._draw_kappa is True:
            kappa_ext_draw = self._kappa_dist.draw_one
        elif self._kappa_ext_bias is True:
            kappa_ext_draw = np.random.normal(kappa_ext, kappa_ext_sigma)
        else:
            kappa_ext_draw = 0
        return lambda_mst_draw, kappa_ext_draw, gamma_ppn
