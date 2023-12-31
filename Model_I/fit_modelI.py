#from astropy import astropy.units as u
from astropy import coordinates as coord
from galpy import orbit
from astropy import units
from galpy.potential import MWPotential2014 as pott
import numpy as np
import csv
from scipy.signal import savgol_filter
from scipy import interpolate
from scipy import stats
from scipy.stats import (multivariate_normal as mvnnorm)
import pickle
from scipy.stats import skewnorm
from scipy.integrate import quad
from scipy.optimize import leastsq
from scipy.stats._multivariate import _squeeze_output
try:
    from sklearn.neighbors import KernelDensity
except ImportError as e:
    print(e)
    print("> WARNING: failed to import sklearn; continuing anyway")
try:
    import psutil # check memory usage
except ImportError as e:
    print(e)
    print("> WARNING: failed to import psutil; continuing anyway")
from lmfit import Model
import matplotlib.pyplot as plt
import pandas as pd
from astroquery.simbad import Simbad
from astropy.io import ascii
from astropy.table import vstack
from lmfit import minimize, Parameters,fit_report, Minimizer
import lmfit
import pybroom as br
import corner
import gc
import sys
from matplotlib.ticker import (MultipleLocator,
                               FormatStrFormatter,
                               AutoMinorLocator,
                               MaxNLocator,
                               AutoLocator)


def hdi(trace, mass_frac):
    """
    source:
    http://bebi103.caltech.edu.s3-website-us-east-1.amazonaws.com/2015/tutorials/l06_credible_regions.html

    Returns highest probability density region given by
    a set of samples.

    Parameters
    ----------
    trace : array
        1D array of MCMC samples for a single variable
    mass_frac : float with 0 < mass_frac <= 1
        The fraction of the probability to be included in
        the HPD.  For example, `massfrac` = 0.95 gives a
        95% HPD.

    Returns
    -------
    output : array, shape (2,)
        The bounds of the HPD
    """
    # Get sorted list
    d = np.sort(np.copy(trace))

    # Number of total samples taken
    n = len(trace)

    # Get number of samples that should be included in HPD
    n_samples = np.floor(mass_frac * n).astype(int)

    # Get width (in units of data) of all intervals with n_samples samples
    int_width = d[n_samples:] - d[:n-n_samples]

    # Pick out minimal interval
    min_int = np.argmin(int_width)

    # Return interval
    return np.array([d[min_int], d[min_int+n_samples]])

def mode_and_HDI(arr, p=0.6827, smooth=True,
                 plot=False, pthres=0.05, return_hist=False,
                 xlabel=None, ylabel=None, vline=None,
                 nbins=None):
    """
    Compute the mode and highest density interval (HDI) of a unimodal distribution,
    in rough analogy to Andreas Irrgang's ISIS function with the same name.

    This function computes the mode and highest density interval (HDI) of
    a unimodal distribution 'arr' of numbers. The mode is the most frequent
    value in the distribution while the HDI indicates which points of the
    distribution are most credible, and which cover most of the distribution.
    Thus, the HDI summarizes the distribution by specifying a probability
    interval that covers a certain fraction 'p' (see the respective argument)
    of the distribution such that every point inside the interval has higher
    credibility than any point outside the interval.

    The output of this function is a dict containing the fields
    - "mode", the mode of the distribution,
    - "HDI_lo", the lower bound of the HDI,
    - "HDI_hi", the upper bound of the HDI,
    - "median", the median of the distribution,
    - "quantile_lo", the (1-p)/2 quantile,
    - "quantile_hi", the 1-(1-p)/2 quantile,
    - "p_s", the fraction of the distribution the HDI is supposed to cover,
    - "p_a", the fraction of the distribution the HDI actually covers.

    Owing to numerical details of the underlying method, the values of 'p_s'
    and 'p_a' will, in general, be close to each other, but not identical. A
    major discrepancy between those two numbers indicates that something went
    wrong. For instance, the underlying algorithm is not capable of properly
    dealing with bimodal distributions. In such cases, the inferred HDI will be
    wrong and not cover the fraction of the distribution it is supposed to cover.
    'pthres' is the maximum allowed difference before nans will be returned.
    """

    narr_in = len(arr)

#    process = psutil.Process(os.getpid())
#    uram = process.memory_info().rss / 1e9; print("> RAM used: %.3f GB" % (uram))

    # quantiles are used with the median
    median = np.median(arr)
    std = np.std(arr)
    pl = 0.+0.5*(1.-p)
    ph = 1.-0.5*(1.-p)
    quantile_lo = np.quantile(arr, pl)
    quantile_hi = np.quantile(arr, ph)
    qr = quantile_hi - quantile_lo

    # the highest density intervales are used with the mode
    HDI_lo, HDI_hi = hdi(arr, p)
    HDI_r = HDI_hi - HDI_lo

    arr_cut = 4
    if p > 0.6: arr_cut = 2
    if p > 0.89: arr_cut = 1
    if p > 0.989: arr_cut = 0.5
    if p > 0.9989: arr_cut = 0.25

    arr = arr[np.where(arr > HDI_lo-HDI_r*arr_cut)]
    arr = arr[np.where(arr < HDI_hi+HDI_r*arr_cut)]
    narr = len(arr)
    nremoved = narr_in - narr

    # create histogram -> numpy often uses too many bins -> too much RAM
    if narr < 0:
        bin_edges = np.histogram_bin_edges(arr, bins='auto')
        hist, _ = np.histogram(arr, bin_edges, density=True)
    else:
#        nbins = min(max(int(np.sqrt(narr)/2+0.5), 10), int(2e4))
        if nbins is None:
            nbins = min(max(int(np.cbrt(narr)*2+0.5), 10), int(2e4))
#        print("> using %d bins" % nbins)
        bmin = HDI_lo-arr_cut*HDI_r
        bmax = HDI_hi+arr_cut*HDI_r
        bin_edges = np.linspace(bmin, bmax, nbins)
        hist, _ = np.histogram(arr, bin_edges, density=True)
        uneven_spacing = True
        if p < 0.7: uneven_spacing = False
        if uneven_spacing:
            # even spacing is inefficient -> thighter spacing based on 
            # hdi_lo, hdi_hi, mode
            # gaussian-spaced
            niter = 3
            for _ in range(niter):
                bin_centers = (bin_edges[:-1] + bin_edges[1:])/2
                imax = np.argmax(hist)
                del hist; gc.collect()
                mode = bin_centers[imax]
                del bin_centers; gc.collect()
                # use a relatively broad gaussian -> almost even spacing
                sigma_gauss = 2*(HDI_hi-HDI_lo)
                # higher concentration around mode for higher p
                sigma_gauss *= (0.6827 / p)**8
                dist = stats.norm(loc=mode, scale=sigma_gauss)
                bounds = dist.cdf([bmin, bmax])
                pp = np.linspace(*bounds, num=nbins)
                bin_edges = dist.ppf(pp)
                bin_edges = bin_edges[np.where(np.isfinite(bin_edges))]
                hist, _ = np.histogram(arr, bin_edges, density=True)
    del arr; gc.collect()
    bin_centers = (bin_edges[:-1] + bin_edges[1:])/2

#    uram = process.memory_info().rss / 1e9; print("> RAM used: %.3f GB" % (uram))

    # integrate from HDI_lo to HDI_hi
    ilo = np.argmax(bin_centers>=HDI_lo)
    ihi = np.argmin(bin_centers<=HDI_hi)
    p_a = np.sum(np.diff(bin_edges[ilo:ihi+1])*hist[ilo:ihi])
    del bin_edges; gc.collect()
    # correct for array entries removed at the very beginning
    p_a *= 1 - nremoved/narr_in

    dout = {}

    if plot:
        figsize = np.array([8, 6])*1.3
        lw = 2.5
    if smooth:
        plot_CDF = False
        if plot:
            if plot_CDF:
                cdf = hist.cumsum()
                cdf /= cdf[-1]
                plt.plot(bin_centers, cdf, 'k--',
                         linewidth=1.5)
                plt.show()

            fig = plt.figure(figsize=figsize)
            ax = plt.gca()
            ax.step(bin_centers, hist, where="mid", color="grey", lw=lw)
        if return_hist:
            dout.update({'hist': hist})
        filter_width = 15
        if narr < 9e6:
            filter_width = 13
        if narr < 5e6:
            filter_width = 11
        if narr < 1e6:
            filter_width = 9
        if narr < 1e5:
            filter_width = 5
        if narr < 2e4:
            filter_width = 3
        filter_order = 1
        if filter_width >= 3:
            filter_order = 2
        hist = savgol_filter(hist, filter_width, filter_order)
        increase_sampling = True
        if increase_sampling:
            nx = len(bin_centers)
            fac_increase = 5
            nnew = int(max(nx*fac_increase, 5e5))
            x_new = np.linspace(np.min(bin_centers), np.max(bin_centers),
                                nnew)
#            hist = np.interp(x_new, bin_centers, hist)
            intp = interpolate.Akima1DInterpolator(bin_centers, hist)
            hist = intp(x_new)
            bin_centers = x_new

    imax = np.argmax(hist)
    if (not plot) and (not return_hist):
        del hist; gc.collect()
    mode = bin_centers[imax]
    if (not plot) and (not return_hist):
        del bin_centers; gc.collect()

    dout.update({"median":median,
                 "quantile_lo":quantile_lo,
                 "quantile_hi":quantile_hi,
                 "mode":mode,
                 "HDI_lo":HDI_lo,
                 "HDI_hi":HDI_hi})

    failed = False
    dout.update({"p_s":p, "p_a":p_a})
    funct_name = sys._getframe().f_code.co_name
    if abs(p_a - p) > pthres:
        wstr = "Warning in '%s': 'p_s' (%g) and 'p_a' (%g) differ by more than %g. \
Something went wrong. Returning np.nan." % \
               (funct_name, dout["p_s"], dout["p_a"], pthres)
        failed = True
    # check if quantiles and mode make sense
    if mode < HDI_lo:
        wstr = "Warning in '%s': 'mode' (%g) < 'HDI_lo' (%g) Something went wrong. \
Returning np.nan." % \
               (funct_name, mode, HDI_lo)
        failed = True
    if mode > HDI_hi:
        wstr = "Warning in '%s': 'mode' (%g) > 'HDI_hi' (%g) Something went wrong. \
Returning np.nan." % \
               (funct_name, mode, HDI_hi)
        failed = True
    if failed:
        print(wstr)
        dout["HDI_lo"] = np.nan
        dout["HDI_hi"] = np.nan
        dout["mode"] = np.nan

    if return_hist:
        dout.update({"bin_centers": bin_centers,
                     "hist_smoothed": hist})

    if plot:
        if not smooth:
            fig = plt.figure(figsize=figsize)
            ax = plt.gca()
        rfac = 1.5
        ax.set_xlim([HDI_lo-(mode-HDI_lo)*rfac, HDI_hi+(HDI_hi-mode)*rfac])
        ax.step(bin_centers, hist, where="mid", color="black", lw=lw)
        del hist; gc.collect()
        ax.axvline(x=quantile_lo, color="gray", ls="--", lw=lw)
        ax.axvline(x=quantile_hi, color="gray", ls="--", lw=lw)
        ax.axvline(x=median, color="black", ls="--", lw=lw)
        ax.axvline(x=HDI_lo, color="orange", lw=lw)
        ax.axvline(x=HDI_hi, color="orange", lw=lw)
        ax.axvline(x=mode, color="red", lw=lw)
        if vline is not None:
            ax.axvline(x=vline, color="blue", lw=lw)
        ax.minorticks_on()
        fontsize = 18
        labelsize = 15
        len_major = 8
        len_minor = 6
        ax.yaxis.set_major_formatter(FormatStrFormatter("%g"))
        ax.tick_params(axis='both',which='major',direction='in',
                       top=True, bottom=True, left=True, right=True,
                       labelsize=labelsize, length=len_major)
        ax.tick_params(axis='both', which='minor', direction='in',
                       top=True, bottom=True, left=True, right=True,
                       length=len_minor)
        if ylabel is None:
            ylabel = r"PDF"
        ax.set_ylabel(ylabel, fontsize=fontsize)
        if xlabel is None:
            xlabel = "x"
        ax.set_xlabel(xlabel, fontsize=fontsize)
        plt.tight_layout()
        plt.show()


    gc.collect()

    return dout

###################
#Define Rotation Curves for 3 components:
##################


def bulgerot(R,Mb,bb):
    vr2=Mb*R*R/pow((R*R+bb*bb),1.5)
    return vr2
def diskrot(R,z,Md,ad,bd):
    zt=pow(z**2+bd**2,0.5)+ad
    den=pow((R*R+zt**2),1.5)
    vr2=Md*R**2/den
    return vr2
def halorot(R,Mh,ah,lam):
    vr2=np.empty(len(R))
    vr2=np.where(R<lam,Mh*R/(ah*(ah+R)),Mh*lam**2/(R*(ah+lam)*ah))
    #for i in range(len(R)):
    #    if R[i]<lam:
    #        vr2[i]=Mh*R[i]/(ah*(ah+R[i]))
    #    else: vr2[i]=Mh*lam*lam/(R[i]*(ah+lam)*ah)
    return vr2


#######################
#Define vertical force from 3 components:
######################

def bulgevert(R,z,Mb,bb):
    num=Mb*z
    den=R*R+z*z+bb*bb
    den=pow(den,1.5)
    vertf=num/den
    return vertf

def diskvert(R,z,Md,ad,bd):
    num=Md*z*(ad+np.sqrt(z*z+bd*bd))
    den1=R*R+pow(ad+np.sqrt(z*z+bd*bd),2)
    den=(np.sqrt(z*z+bd*bd))*pow(den1,1.5)
    vertf=num/den
    return vertf

def halovert(R,z,Mh,ah,lam):
    vertf=np.empty(len(R))
    radius=pow(R*R+z*z,0.5)
    for i in range(len(R)):
        if R[i]<lam:
            vertf[i]=Mh*z/(ah*(ah+radius[i])*radius[i])
        else: vertf[i]=Mh*z*lam*lam/(R[i]*R[i]*R[i]*(ah+lam)*ah)
    return vertf




#########################
#Read Data from files:
########################
Inner=1 #1 is for Reid 2014, 2 for Sofue 2021, 3 is for Sofue 2009

data1 = ascii.read("data/rotcurv_eilers.ascii")
data2 = ascii.read("data/rotcurv_bhatta_25kpc.ascii")

if Inner==1:
    name="Reid(2014)"
    data3 = ascii.read("data/rotcurv_reid_1to5.ascii")
elif Inner==2:
    name="Sofue(2021)"
    data3 = ascii.read("data/rotcurv_sofue_2021_5kpc.ascii")
elif Inner==3:
    name="Sofue(2009)"
    data3 = ascii.read("data/rotcurv_sofue_2009_5kpc.ascii")

data=vstack([data1,data2,data3])
data4 = ascii.read("data/vertif.ascii")


radius1=data1['R']
vel1=data1['vc']
poser1=data1['sigmaplus']
neger1=data1['sigmaminus']
len1=len(radius1)

radius2=data2['R']
vel2=data2['vc']
poser2=data2['sigmaplus']
neger2=data2['sigmaminus']
len2=len(radius2)

radius3=data3['R']
vel3=data3['vc']
poser3=data3['sigmaplus']
neger3=data3['sigmaminus']
len3=len(radius3)

radius=data['R']
vel=data['vc']
poser=data['sigmaplus']
neger=data['sigmaminus']

radius4=data4['R']
vertif=data4['K']
erforce=data4['delK']
len4=len(radius4)

weights=np.zeros(len(radius)+len(radius4))
weights[0:len1]=1.0/np.sqrt(len1)
weights[len1:(len1+len2)]=1.0/np.sqrt(len2)
weights[(len1+len2):(len1+len2+len3)]=1.0/np.sqrt(len3)
weights[(len1+len2+len3):]=1.0/np.sqrt(len4)


########################
#Functions for fitting models:
#######################
#radiusformodel=np.linspace(4.0,10.0,100)
def rotcurve(R,mb,bb,md,ad,bd,mh,ah,lam):
        vr2tot=bulgerot(R,mb,bb)+diskrot(R,0,md,ad,bd)+halorot(R,mh,ah,lam)
        return 10*pow(vr2tot,0.5)
def vertforce(R,z,mb,bb,md,ad,bd,mh,ah,lam):
    vertftot=bulgevert(R,z,mb,bb)+diskvert(R,z,md,ad,bd)+halovert(R,z,mh,ah,lam)
    return 100*vertftot/(2*3.14*4.30091)
#########################
#Function for density:#
import sys, os
sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/../Galactic_functions")
from functions import bulge_density,disk_density,halo_as_density

###The original density function is based on x,y,z but here we take x=r and put y as 0
def density(R,z,mb,bb,md,ad,bd,mh,ah,lam):
        diskden=disk_density(R,0,z,md,ad,bd)
        bulgeden=bulge_density(R,0,z,mb,bb)
        haloden=halo_as_density(R,0,z,mh,ah,lam)
        den=diskden+bulgeden+haloden
        return den/(40.003)

#############################


################################
#Deinfing the Residuals and fitting:
#################################3
def residual(params, x1,x2, vel, uncertainty1,vertif,erforce):
        
        x=np.append(x1,x2) 
        resid=0.0*x[:]
        mb = params['mb']
        bb = params['bb']
        md = params['md']
        ad = params['ad']
        bd = params['bd']
        mh = params['mh']
        lam =params['lam']
        ah= params['ah']
        model1 = rotcurve(x1,mb,bb,md,ad,bd,mh,ah,lam)
        model2 = vertforce(x2,1.1,mb,bb,md,ad,bd,mh,ah,lam)
        density_solar=density(8.178,0,mb,bb,md,ad,bd,mh,ah,lam)
        halo_density_solar=halo_as_density(8.178,0,0,mh,ah,lam)/(40.003)
        
        
        for i in range(len(x)):#Calculate the residuals for the functions separately
            if i<len(x1):
                resid[i]=weights[i]*(vel[i]-model1[i]) / uncertainty1[i]
            else:
                resid[i]=weights[i]*(vertif[i-len(x1)]-model2[i-len(x1)]) / erforce[i-len(x1)]
        
        density_residual=(0.097-density_solar)/0.015
        halo_density_residual=(0.013-halo_density_solar)/0.005
        
        residual=np.append(resid,np.append(density_residual,halo_density_residual))
        
        return residual

###################################################
###Fitting Procedure:
np.random.seed(1)
steps=1000
walkers=20
params = Parameters()
params.add('mb', value=409,min=0) #Initial values for Params from Irrgang 2013
params.add('bb', value=0.23,min=0,max=5)
params.add('md', value=2856,min=0)
params.add('ad', value=4.22,min=0,max=10)
params.add('bd', value=0.292,min=0)
params.add('mh', value=1018,min=0)
params.add('ah', value=2.562,min=0,max=10)
params.add('lam', value=100,min=0,max=200)

mini = lmfit.Minimizer(residual, params,(radius,radius4,vel, (poser+neger)/2,vertif,erforce), nan_policy='omit') #The first minimizer which uses ampgo followed by the second one
out=mini.minimize(method="ampgo",params=params)
out3=mini.minimize(method="emcee",params=out.params,steps=steps,burn=100,nwalkers=walkers,is_weighted=True)
print(fit_report(out))
print(fit_report(out3))
flatchain = out3.flatchain
np.save('flatchain.npy', flatchain)
np.savetxt('flatchain.csv', flatchain, delimiter=',')
'''
#########################################################
file=open('fitreports/fitreports_final/fitreport_model1_%s_%s_%s.dat'%(name,steps,walkers),'w')
file.write(fit_report(out))
file.write(fit_report(out3))
file.write("\n")
mcmc_mod=out3.params.copy()
mcmc_mxl=out3.params.copy()
highest_prob = np.argmax(out3.lnprob)
hp_loc = np.unravel_index(highest_prob, out3.lnprob.shape)
mle_soln = out3.chain[hp_loc]
data = []


print('\nError estimates from emcee:')

for ix, name2 in enumerate(out3.params.copy()):
    print(name2)
    MC_array = np.array(out3.flatchain[name2])
    mode = mode_and_HDI(MC_array, plot=True)
    mcmc_mod[name2].value=mode["mode"]
    quantiles = np.percentile(out3.flatchain[name2],
                              [2.275, 15.865, 50, 84.135, 97.275])
    median = quantiles[2]
    err_m2 = quantiles[0] - median
    err_m1 = quantiles[1] - median
    err_p1 = quantiles[3] - median
    err_p2 = quantiles[4] - median
    fmt = '  {:5s}   {:8.4f} {:8.4f} {:8.4f} {:8.4f} {:8.4f}'.format
    #print(fmt(name2, err_m2, err_m1, median, err_p1, err_p2))
    file.write(fmt(name2, err_m2, err_m1, median, err_p1, err_p2))
    file.write("\n")
    fmt = '{:8.4f} {:8.4f} {:8.4f}'.format
    file.write(fmt(mode["mode"],-(mode["HDI_lo"]-mode["mode"]),(mode["HDI_hi"]-mode["mode"])))
    file.write("\n")
    #print(f"{param}: {mle_soln[ix]:.3f}")
    file.write(f"\n{name2}: {mle_soln[ix]:.3f}\n")
    file.write(f"\n 2 sigma spread = {0.5 * (quantiles[4] - quantiles[0]):.3f}")
    file.write(f"\n\n1 sigma spread = {0.5 * (quantiles[3] - quantiles[1]):.3f}")
    mcmc_mxl[name2].value=mle_soln[ix]
    data.append({'Parameter': name2, 'ampgo': out.params[name2].value,'mcmc_mxl': mcmc_mxl[name2].value,'mcmc_mxl_2sigma_plus': quantiles[4], 'mcmc_mxl_2sigma_minus':  quantiles[0],'mcmc_mode': mcmc_mod[name2].value,'mcmc_mode_hdi_plus':(mode["HDI_hi"]-mode["mode"]),'mcmc_mode_hdi_minus': (mode["HDI_lo"]-mode["mode"])})
    

###################################
density_solar_mcmc_mode=density(8.178,0,mcmc_mod['mb'].value,mcmc_mod['bb'].value,mcmc_mod['md'].value,mcmc_mod['ad'].value,mcmc_mod['bd'].value,mcmc_mod['mh'].value,mcmc_mod['ah'].value,mcmc_mod['lam'].value)
density_solar_mcmc_mxl=density(8.178,0,mcmc_mxl['mb'].value,mcmc_mxl['bb'].value,mcmc_mxl['md'].value,mcmc_mxl['ad'].value,mcmc_mxl['bd'].value,mcmc_mxl['mh'].value,mcmc_mxl['ah'].value,mcmc_mxl['lam'].value)

halo_density_solar_mode=halo_as_density(8.178,0,0,mcmc_mod['mh'].value,mcmc_mod['ah'].value,mcmc_mod['lam'].value)
halo_density_solar_mxl=halo_as_density(8.178,0,0,mcmc_mxl['mh'].value,mcmc_mxl['ah'].value,mcmc_mxl['lam'].value)

file.write(f"\n solar density MCMC mode: {density_solar_mcmc_mode[0]}")
file.write(f"\n solar density MCMC mxl:  {density_solar_mcmc_mxl[0]}")

file.write(f"\n solar halo density mode: {halo_density_solar_mode[0]/(40.003)}")
file.write(f"\n solar halo density mxl: {halo_density_solar_mxl[0]/(40.003)}")

file.close()


#################################################

filename = 'output_params/fit_%s_%s_%s_params.csv'%(name,steps,walkers)
with open(filename, 'w', newline='') as file:
    writer = csv.DictWriter(file, fieldnames=['Parameter', 'ampgo', 'mcmc_mxl', 'mcmc_mxl_2sigma_plus', 'mcmc_mxl_2sigma_minus','mcmc_mode','mcmc_mode_hdi_plus','mcmc_mode_hdi_minus'])
    writer.writeheader()
    writer.writerows(data)


#################
#Plotting the results:
#################

radiusformodel=np.linspace(0.01,200.0,2000) #Radius for plotting the rotation cur
radiusforvertf=np.linspace(4.0,10.0,50) #Radius for plotting the vertical force

#####################
vrinitial=rotcurve(radiusformodel,409,0.23,2856,4.22,0.292,1018,2.562,200) #Rotation curve for model of Irrgang 2013

vrmodel_mcmc_mod=rotcurve(radiusformodel,mcmc_mod['mb'].value,mcmc_mod['bb'].value,mcmc_mod['md'].value,mcmc_mod['ad'].value,mcmc_mod['bd'].value,mcmc_mod['mh'].value,mcmc_mod['ah'].value,mcmc_mod['lam'].value)#Rotation curve for MCMC Mode values

vrmodel_ampgo=rotcurve(radiusformodel,out.params['mb'].value,out.params['bb'].value,out.params['md'].value,out.params['ad'].value,out.params['bd'].value,out.params['mh'].value,out.params['ah'].value,out.params['lam'].value)#Rotation curve for AMGO best fit

vrmodel_mcmc_mxl=rotcurve(radiusformodel,mcmc_mxl['mb'].value,mcmc_mxl['bb'].value,mcmc_mxl['md'].value,mcmc_mxl['ad'].value,mcmc_mxl['bd'].value,mcmc_mxl['mh'].value,mcmc_mxl['ah'].value,mcmc_mxl['lam'].value) #Rotation curve for MCMC maximum likelihood

vrmodel_mcmc_b=10*np.sqrt(bulgerot(radiusformodel,mcmc_mod['mb'].value,mcmc_mod['bb'].value))
vrmodel_mcmc_d=10*np.sqrt(diskrot(radiusformodel,0.0,mcmc_mod['md'].value,mcmc_mod['ad'].value,mcmc_mod['bd'].value))
vrmodel_mcmc_h=10*np.sqrt(halorot(radiusformodel,mcmc_mod['mh'].value,mcmc_mod['ah'].value,mcmc_mod['lam'].value))
######################

vertinitial=vertforce(radiusforvertf,1.1,409,0.23,2856,4.22,0.292,1018,2.562,200)  #Vertical force for model of Irrgang 2013
vertmodel_mcmc_mod=vertforce(radiusforvertf,1.1,mcmc_mod['mb'].value,mcmc_mod['bb'].value,mcmc_mod['md'].value,mcmc_mod['ad'].value,mcmc_mod['bd'].value,mcmc_mod['mh'].value,mcmc_mod['ah'].value,mcmc_mod['lam'].value)#Vertical force for our best fit 
vertmodel_ampgo=vertforce(radiusforvertf,1.1,out.params['mb'].value,out.params['bb'].value,out.params['md'].value,out.params['ad'].value,out.params['bd'].value,out.params['mh'].value,out.params['ah'].value,out.params['lam'].value)#Vertical force for our best fit 
vertmodel_mcmc_mxl=vertforce(radiusforvertf,1.1,mcmc_mxl['mb'].value,mcmc_mxl['bb'].value,mcmc_mxl['md'].value,mcmc_mxl['ad'].value,mcmc_mxl['bd'].value,mcmc_mxl['mh'].value,mcmc_mxl['ah'].value,mcmc_mxl['lam'].value)#Vertical force for our best fit 



###RotCurve:
plt.figure(0)
plt.xlabel('log(R) (kpc)')
plt.ylabel('V$_{circ}$')
plt.plot(np.log10(radiusformodel), vrinitial, 'k',label="Best Fit (Irrgang 2013)")
plt.plot(np.log10(radiusformodel), vrmodel_mcmc_mod, 'r^', label='Best fit (MCMC Mode)',markersize=1)
plt.plot(np.log10(radiusformodel), vrmodel_mcmc_mxl, 'kv', label='Best fit (MCMC MXL)',markersize=1)
plt.plot(np.log10(radiusformodel), vrmodel_ampgo, 'r--', label='Best fit (AMPGO)',markersize=1)
#plt.plot(np.log10(radiusformodel), vrmodel_mcmc_b, 'k--', label='Bulge',markersize=2)
#plt.plot(np.log10(radiusformodel), vrmodel_mcmc_d, 'k+', label='Disk',markersize=2)
#plt.plot(np.log10(radiusformodel), vrmodel_mcmc_h, 'ko', label='Halo',markersize=2)
plt.errorbar(np.log10(radius1), vel1, yerr=(poser1,neger1),fmt='.', label='Eilers(2019)')
plt.errorbar(np.log10(radius2), vel2, yerr=(poser2,neger2),fmt='+', label='Bhattacharjee(2014)')
plt.errorbar(np.log10(radius3), vel3, yerr=(poser3,neger3),fmt='s', label=name)
plt.legend(loc='upper left',fontsize=9)
plt.savefig("Plots/%s/Rotcur_ModelI_eil_bhatta_log_%s_%s.pdf"%(name,steps,walkers),bbox_inches="tight")
###RotCurve:
plt.figure(1)
plt.xlabel('R (kpc)')
plt.ylabel('V$_{circ}$')
plt.plot(radiusformodel[50:], vrinitial[50:], 'k',label="Best Fit (Irrgang 2013)")
plt.plot(radiusformodel[50:], vrmodel_mcmc_mod[50:], 'r^', label='Best fit (MCMC Mode)',markersize=1)
plt.plot(radiusformodel[50:], vrmodel_mcmc_mxl[50:], 'kv', label='Best fit (MCMC MXL)',markersize=1)
plt.plot(radiusformodel[50:], vrmodel_ampgo[50:], 'r--', label='Best fit (AMPGO)',markersize=1)
plt.errorbar(radius1, vel1, yerr=(poser1,neger1),fmt='.', label='Eilers(2019)')
plt.errorbar(radius2, vel2, yerr=(poser2,neger2),fmt='+', label='Bhattacharjee(2014)')
plt.legend()
plt.savefig("Plots/%s/Rotcur_ModelI_eil_bhatta_%s_%s.pdf"%(name,steps,walkers))


####Residuals (Use log for radius when working with rotation curve):
plt.figure(2)
plt.errorbar(radius4, out.residual[len(radius):(len(radius)+len(radius4))],fmt='+', label='Ampgo Residual')
plt.axhline(y=0,color='r')
plt.xlabel('R(kpc)')
plt.ylabel('Residuals')
plt.savefig("Plots/%s/Residuals_vertif_%s_%s.pdf"%(name,steps,walkers))
###Vertical Force:
plt.figure(3)
plt.xlabel('R (kpc)')
plt.ylabel('F$_{v}$')
plt.plot(radiusforvertf, vertinitial, 'k',label="Best Fit (Irrgang et.al. 2013)")
plt.plot(radiusforvertf, vertmodel_mcmc_mod, 'r', label='Best fit (MCMC Mode)')
plt.plot(radiusforvertf, vertmodel_mcmc_mxl, 'ro', label='Best fit (MCMC Mxl)')
plt.plot(radiusforvertf, vertmodel_ampgo, 'r--', label='Best fit (AMPGO)')
plt.errorbar(radius4,vertif,yerr=(erforce/2,erforce/2),fmt= '+', label='Bovy et. al.')
plt.legend()
plt.savefig("Plots/%s/VertForce_ModelI_%s_%s.pdf"%(name,steps,walkers))
########
plt.figure(4)
plt.plot(out3.acceptance_fraction, 'o')
plt.xlabel('walker')
plt.ylabel('acceptance fraction')
plt.savefig("Plots/%s/Acceptance_%s_%s.pdf"%(name,steps,walkers))
###########

plt.figure(5)
emcee_corner = corner.corner(out3.flatchain, labels=out3.var_names,truths=list(out3.params.valuesdict().values()),quantiles=[0.16,0.5,0.84])
emcee_corner.savefig("Plots/%s/emcee_corner_%s_%s.pdf"%(name,steps,walkers))




'''




