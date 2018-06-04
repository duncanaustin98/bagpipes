from __future__ import print_function, division, absolute_import

import numpy as np
import sys

from scipy.optimize import fsolve
from copy import copy, deepcopy

from . import utils
from . import plotting
from .chemical_enrichment_history import chemical_enrichment_history


def lognorm_equations(p, consts):

    tau_solve, T0_solve = p

    xmax, h = consts

    tau = np.exp(T0_solve - tau_solve**2) - xmax
    t0 = xmax*(np.exp(0.5*np.sqrt(8*np.log(2)*tau_solve**2))
               - np.exp(-0.5*np.sqrt(8*np.log(2)*tau_solve**2))) - h

    return (tau, t0)


class star_formation_history:
    """ Generate a star formation history.

    Parameters
    ----------

    model_comp : dict
        A dictionary containing information about the star formation
        history you wish to generate.

    log_sampling : float (optional)
        the log of the age sampling of the SFH, defaults to 0.0025.
    """

    def __init__(self, model_comp, log_sampling=0.0025):

        if utils.model_type not in list(utils.ages):
            utils.set_model_type(utils.model_type)

        self.hubble_time = utils.age_at_z[utils.z_array == 0.]

        # This has to be a little bigger than the hubble time or the 
        # unphysical flag is never set for models at z = 0.
        log_age_max = np.log10(self.hubble_time)+9. + 2*log_sampling
        self.ages = np.arange(6., log_age_max, log_sampling)
        self.age_lhs = utils.make_bins(self.ages, make_rhs=True)[0]

        self.ages = 10**self.ages
        self.age_lhs = 10**self.age_lhs

        self.age_lhs[0] = 0.
        self.age_lhs[-1] = 10**9*self.hubble_time

        self.age_widths = self.age_lhs[1:] - self.age_lhs[:-1]

        self.sfr = {"total": np.zeros_like(self.ages)}
        self.weights = {"total": np.zeros_like(utils.chosen_ages)}

        # Populate a list of star-formation history components
        self.sfh_components = []
        self.comp_types = []

        for comp in list(model_comp):
            if (not comp.startswith(("dust", "nebular", "polynomial"))
                    and isinstance(model_comp[comp], dict)):

                comp_type = copy(comp)
                while comp_type[-1].isdigit():
                    comp_type = comp_type[:-1]

                self.comp_types.append(comp_type)

                self.sfh_components.append(comp)
                self.sfr[comp] = np.zeros_like(self.ages)
                self.weights[comp] = np.zeros_like(utils.chosen_ages)

        self.update(model_comp)

    def _mass_calculations(self):
        """ Calculate the living and formed stellar masses for a SFH
        component, also keeps track of the total for all components. """

        for name in self.sfh_components:
            living_mass_grid = utils.chosen_live_frac
            
            zmet_weights = np.expand_dims(self.ceh.zmet_weights[name], 1)
            sfh_weights = self.weights[name]

            living_mass_by_age = np.sum(zmet_weights*living_mass_grid, axis=0)
            living_mass = np.sum(np.squeeze(sfh_weights)*living_mass_by_age)

            formed_mass = 10**self.model_comp[name]["massformed"]

            self.mass["total"]["living"] += living_mass
            self.mass["total"]["formed"] += formed_mass
            self.mass[name] = {"living": living_mass, "formed": formed_mass}

    def update(self, model_comp):

        self.model_comp = model_comp

        self.unphysical = False

        self.age_of_universe = 10**9*np.interp(self.model_comp["redshift"],
                                               utils.z_array, utils.age_at_z)

        # ceh: Chemical enrichment history object
        self.ceh = chemical_enrichment_history(self.model_comp)

        # mass: stores component and total stellar masses.
        self.mass = {"total": {"formed": 0., "living": 0.}}

        self.sfr["total"] = np.zeros_like(self.ages)
        self.weights["total"] = np.zeros_like(utils.chosen_ages)

        # Calculate the star-formation history for each of the components.
        for i in range(len(self.sfh_components)):
            comp = self.sfh_components[i]
            comp_type = self.comp_types[i]

            getattr(self, comp_type)(self.sfr[comp], self.model_comp[comp])

            # Normalise to the correct mass.
            self.sfr[comp] /= np.sum(self.sfr[comp]*self.age_widths)
            self.sfr[comp] *= 10**self.model_comp[comp]["massformed"]
            self.sfr["total"] += self.sfr[comp]

        # Check that no stars formed before the Big Bang.
        mask = self.ages > self.age_of_universe
        if not np.max(mask) == 0 and self.sfr["total"][mask].max() > 0.:
            self.unphysical = True

        # Sum up the contributions to each age bin to create SSP weights.
        for comp in self.sfh_components:
            comp_weights = self.sfr[comp]*self.age_widths
            self.weights[comp] = np.histogram(self.ages,
                                              bins=utils.chosen_age_lhs,
                                              weights=comp_weights)[0]

            self.weights["total"] += self.weights[comp]

        # Calculate living and formed stellar mass contributions.
        self._mass_calculations()

        self.sfr_100myr = np.sum(self.sfr["total"][self.ages < 10**8]
                                 * self.age_widths[self.ages < 10**8])

        self.sfr_100myr /= self.age_widths[self.ages < 10**8].sum()

        weighted_ages = self.sfr["total"]*self.age_widths*self.ages
        self.mass_weighted_age = np.sum(weighted_ages)
        self.mass_weighted_age /= np.sum(self.sfr["total"]*self.age_widths)

    def burst(self, sfr, param):
        """ A delta function burst of star-formation. """

        age = param["age"]*10**9
        sfr[np.argmin(np.abs(self.ages - age))] += 1

        if age > self.age_of_universe:
            self.unphysical = True

    def constant(self, sfr, param):
        """ Constant star-formation between some limits. """

        age_max = param["age_max"]*10**9
        age_min = param["age_min"]*10**9

        mask = (self.ages > age_min) & (self.ages < age_max)
        sfr[mask] += 1.

    def exponential(self, sfr, param):

        age = param["age"]*10**9
        tau = param["tau"]*10**9

        t = age - self.ages[self.ages < age]

        sfr[self.ages < age] = np.exp(-t/tau)

    def delayed(self, sfr, param):

        age = param["age"]*10**9
        tau = param["tau"]*10**9

        t = age - self.ages[self.ages < age]

        sfr[self.ages < age] = t*np.exp(-t/tau)

    def const_exp(self, sfr, param):

        age = param["age"]*10**9
        tau = param["tau"]*10**9

        t = age - self.ages[self.ages < age]

        sfr[self.ages < age] = np.exp(-t/tau)
        sfr[(self.ages > age) & (self.ages < self.age_of_universe)] = 1.

    def lognormal(self, sfr, param):
        if "tmax" in list(param) and "fwhm" in list(param):
            tmax, fwhm = param["tmax"]*10**9, param["fwhm"]*10**9

            tau_guess = fwhm/(2*tmax*np.sqrt(2*np.log(2)))
            t0_guess = np.log(tmax) + fwhm**2/(8*np.log(2)*tmax**2)

            tau, t0 = fsolve(lognorm_equations, (tau_guess, t0_guess),
                             args=([tmax, fwhm]))

        else:
            tau, t0 = par_dict["tau"], par_dict["t0"]

        mask = self.ages < self.age_of_universe
        t = self.age_of_universe - self.ages[mask]

        sfr[mask] = ((1./np.sqrt(2.*np.pi*tau**2))*(1./t)
                     * np.exp(-(np.log(t) - t0)**2/(2*tau**2)))

    def dblplaw(self, sfr, param):
        alpha = param["alpha"]
        beta = param["beta"]
        tau = param["tau"]*10**9

        mask = self.ages < self.age_of_universe
        t = self.age_of_universe - self.ages[mask]

        sfr[mask] = ((t/tau)**alpha + (t/tau)**-beta)**-1

        if tau > self.age_of_universe:
            self.unphysical = True

    def custom(self, sfr, param):
        history = param["history"]
        if isinstance(history, str):
            custom_sfh = np.loadtxt(history)

        else:
            custom_sfh = history

        sfr[:] = np.interp(self.ages, custom_sfh[:, 0], custom_sfh[:, 1],
                           left=0, right=0)

        sfr[self.ages > self.age_of_universe] = 0.

    def plot(self, show=True, style="smooth"):
        return plotting.plot_sfh(self, show=show, style=style)
