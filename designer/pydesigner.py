"""
Runs the PyDesigner pipeline
"""

#----------------------------------------------------------------------
# Package Management
#----------------------------------------------------------------------
import sys as sys
import subprocess #subprocess
import os # mkdir
import os.path as op # path
import shutil # which, rmtree
import gzip # handles fsl's .gz suffix
import argparse # ArgumentParser, add_argument
import textwrap # dedent
import numpy as np # array, ndarray
from designer.preprocessing import util, smoothing, rician, preparation, snrplot, mrinfoutil, mrpreproc
from designer.fitting import dwipy as dp
from designer.system import systemtools as systools
from designer.postprocessing import filters
DWIFile = util.DWIFile
DWIParser = util.DWIParser

# Locate mrtrix3 via which-ing dwidenoise
dwidenoise_location = shutil.which('dwidenoise')
if dwidenoise_location == None:
    raise Exception('Cannot find mrtrix3, please see '
        'https://github.com/m-ama/PyDesigner/wiki'
        ' to troubleshoot.')

# Extract mrtrix3 path from dwidenoise_location
mrtrix3path = op.dirname(dwidenoise_location)

# Locate FSL via which-ing fsl
fsl_location = shutil.which('fsl')
if fsl_location == None:
    raise Exception('Cannot find FSL, please see '
        'https://github.com/m-ama/PyDesigner/wiki'
        ' to troubleshoot.')

# Extract FSL path from fsl_location
fslpath = op.dirname(fsl_location)

# Configure system for Intel MKL
if systools.isAMD():
    systools.setenv([('MKL_DEBUG_CPU_TYPE','5')])

def main():
    #----------------------------------------------------------------------
    # Parse Arguments
    #----------------------------------------------------------------------
    # Initialize ArgumentParser
    parser = argparse.ArgumentParser(
            prog='pydesigner',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=textwrap.dedent('''\
    Appendix
    --------
    Filename note:
        Use the base name without the extension. This makes it easy to program
        in automatic .bvec/.bval detection for Niftis and makes your shell
        easier to read by others. The program will automatically search image
        filenames for .nii and .nii.gz extensions. If you use the --dicom
        option, then the program will assume that the entire directory
        consists of dicom files, and will warn you of any files which fail to
        be read in as dicoms.

    Example usage:
        In order to process in the standard way:
        python3 pydesigner.py \\
                --standard \\
                <dwi>

        In order to process in a custom pipeline with denoising, eddy, reverse
        phase encoding, and smoothing, but no diffusion metrics:
        python3 pydesigner.py \\
                --denoise \\
                --eddy \\
                --rpe_pair <rpe> \\
                --pe_dir <dir> \\
                --smooth \\
                <dwi>

        In order to just do denoising, eddy with reverse phase encode, and 
        diffusion metrics:
        python3 pydesigner.py \\
                --denoise \\
                --eddy \\
                --rpe_pair <rpe> \\
                --pe_dir <dir> \\
                --DKI \\
                <dwi>

    Standard pipeline steps:
        1. dwidenoise (thermal denoising)
        2. mrdegibbs (gibbs unringing)
        3. topup + eddy (undistortion)
        4. b1 bias correction
        4. CSF-excluded smoothing
        5. rician bias correction
        6. normalization to white matter in first b0 image
        7. IRWLLS, CWLLS DKI fit
        8. Outlier detection and removal

    See also:
        GitHub      https://github.com/m-ama/PyDesigner
        mrtrix3     https://www.mrtrix.org/
        fsl         https://fsl.fmrib.ox.ac.uk/fsl/fslwiki

                                    '''))

    # Specify arguments below

    # Mandatory
    parser.add_argument('dwi', help='the diffusion dataset you would like to '
                        'process',
                        type=str)

    # Optional
    parser.add_argument('-o', '--output',
                        help='Output location. '
                        'Default: same path as dwi.',
                        type=str)
    parser.add_argument('-s', '--standard', action='store_true',
                        default=False,
                        help='Standard preprocessing, bypasses most other '
                        'options. See Appendix:Standard pipeline steps '
                        'for more information. ')
    parser.add_argument('--out_all', action='store_true',
                        default=False,
                        help='Output NifTi formatted files at the '
                        'end of each preprocessing step.')
    parser.add_argument('--denoise', action='store_true', default=False,
                        help='Run thermal denoising with dwidenoise.')
    parser.add_argument('--extent', metavar='n,n,n', default='5,5,5',
                        help='Denoising extent formatted n,n,n (forces '
                        ' denoising. '
                        'Default: 5,5,5.')
    parser.add_argument('--degibbs', action='store_true', default=False,
                        help='Perform gibbs unringing. Only perform if you '
                        'have full Fourier encoding. The program will check '
                        'for you if you have a .json sidecar.')
    parser.add_argument('--undistort', action='store_true', default=False,
                        help='Run FSL eddy to perform image undistortion. '
                        'NOTE: needs a --topup to run.')
    parser.add_argument('--topup', default=None,
                        help='The topup b0 series with a reverse phase encode '
                        'direction opposite the dwi. REQUIRED for '
                        '--undistort')
    parser.add_argument('--smooth', action='store_true', default=False,
                        help='Perform smoothing on the DWI data. '
                        'Recommended to also supply --csfmask in order to '
                        'avoid contaminating the voxels which border CSF.')
    parser.add_argument('--fwhm', type=float,
                        help='The FWHM to use as a multiple of voxel size. '
                        'Default 1.25')
    parser.add_argument('--kernel', metavar='n,n,n', default='3,3,3',
                        help='Smoothing kernell formatted n,n,n.'
                        'Default: 5,5,5.')
    parser.add_argument('--csfmask', default=None,
                        help='CSF mask for exclusion during smoothing. '
                        'Must be in the DWI space and resolution. ')
    parser.add_argument('--rician', action='store_true', default=False,
                        help='Perform Rician noise correction on the data '
                        '(requires --denoise to generate a noisemap).')
    parser.add_argument('--nofit', action='store_true', default=False,
                        help='Do not fit DTI or DKI tensors.')
    parser.add_argument('--noakc', action='store_true', default=False,
                        help='Do not brute force K tensor outlier rejection.')
    parser.add_argument('--nooutliers', action='store_true', default=False,
                        help='Do not perform outlier correction on kurtosis '
                        'fitting metrics.')
    parser.add_argument('-w', '--wmti', action='store_true', default=False,
                        help='Include DKI WMTI parameters (forces DKI): '
                        'AWF, IAS_params, EAS_params. ')
    parser.add_argument('--kcumulants', action='store_true', default=False,
                        help='output the kurtosis tensor with W cumulant '
                        'rather than K. ')
    parser.add_argument('--mask', action='store_true', default=False,
                        help='Compute a brain mask prior to tensor fitting '
                        'to strip skull and improve efficiency. Optionally, '
                        'use --maskthr to specify a threshold manually.')
    parser.add_argument('--maskthr', metavar='<FA threshold>',
                        help='FSL bet threshold used for brain masking. '
                        'Default: 0.25')
    parser.add_argument('--user_mask', metavar='<brain mask path>',
                        help='Path to user-supplied brain mask.',
                        type=str)
    parser.add_argument('--fit_constraints', default='0,1,0',
                        help='Constrain the WLLS fit. '
                        'Default: 0,1,0.')
    parser.add_argument('--rpe_none', action='store_true', default=False,
                        help='No reverse phase encode is available; FSL eddy '
                        'will perform eddy current and motion correction '
                        ' only. ')
    parser.add_argument('--rpe_pair', metavar='<reverse PE b=0 image>',
                        help='Specify reverse phase encoding image.')
    parser.add_argument('--rpe_all', metavar='<reverse PE dwi>',
                        help='All DWIs have been acquired with an opposite '
                        'phase encoding direction. This information will be '
                        'used to perform a recombination of image volumes '
                        '(each pair of volumes with the same b-vector but '
                        'different phase encoding directions will be '
                        'combined into a single volume). The argument to '
                        'this option is the set of volumes with '
                        'reverse phase encoding but the same b-vectors the '
                        'same as the input image.')
    parser.add_argument('--pe_dir', metavar='<phase encoding direction>',
                        help='Specify the phase encoding direction of the '
                        'input series. NOTE: REQUIRED for eddy due to a bug '
                        'in dwipreproc. Can be signed axis number, (-0,1,+2) '
                        'axis designator (RL, PA, IS), or '
                        'NIfTI axis codes (i-,j,k)')
    parser.add_argument('--noqc', action='store_true', default=False,
                        help='Disable QC saving of QC metrics')
    parser.add_argument('--median', action='store_true', default=False,
                        help='Performs postprocessing median filtering of '
                        'final maps. WARNING: Use on a case-by-case '
                        'basis for bad data only. When applied, the '
                        'filter alters the values of most voxels, so '
                        'it should be used with caution and avoided '
                        'when data quality is otherwise adequate. '
                        'While maps appear visually soother with '
                        'this flag on, they may nonetheless be less '
                        'accurate.')
    parser.add_argument('--nthreads', type=int,
                        help='Number of threads to use for computation. '
                        'Note that using too many threads will cause a slow-'
                        'down.')
    parser.add_argument('--resume', action='store_true',
                        help='Continue from an aborted or partial previous '
                        'run of pydesigner.')
    parser.add_argument('--force', action='store_true',
                        help='Force overwrites of existing files. Otherwise, '
                        'there will be an error at runtime.')
    parser.add_argument('--verbose', action='store_true',
                        help='Print out all output. This is a very messy '
                        'option. We recommend piping output to a text file '
                        'if you use this option.')
    parser.add_argument('--adv', action='store_true',
                        help='Disables safety checks for advanced users who '
                            'want to force a preprocessing step. WARNING: '
                            'THIS FLAG IS FOR ADVANCED USERS ONLY WHO FULLY '
                            'UNDERSTAND THE MRI SYSTEM AND ITS OUTPUTS. '
                            'RUNNING WITH THIS FLAG COULD POTENTIALLY '
                            'RESULT IN IMPRECISE AND INACCURATE RESULTS.')

    # Use argument specification to actually get args
    args = parser.parse_args()

    #---------------------------------------------------------------------
    # Parse Input Image
    #----------------------------------------------------------------------
    image = DWIParser(args.dwi)
    # Variable fType indicates the extension to raw_dwi.X, where X take the
    # place of known dMRI file extensions (.mif, .nii, .nii.gz). This allows
    # easy switching based on any scenario for testing.
    fType = '.mif'
    if not args.output:
        outpath = image.getPath()
    else:
        outpath = args.output
    image.cat(path=outpath,
            ext=fType,
            verbose=args.verbose,
            force=args.force,
            resume=args.resume)
    working_path = op.join(outpath, 'working' + fType)

    # Make an initial conversion to nifti
    init_nii = op.join(outpath, 'dwi_raw.nii')
    mrpreproc.miftonii(input=working_path,
                       output=init_nii,
                       strides='1,2,3,4',
                       nthreads=args.nthreads,
                       force=args.force,
                       verbose=args.verbose)

    #---------------------------------------------------------------------
    # Validate Arguments
    #----------------------------------------------------------------------

    errmsg = ''
    warningmsg = ''
    msgstart = 'Incompatible arguments: '
    override = '; overriding with '
    # Warn if --standard and cherry-picking
    if args.standard:
        stdmsg= '--standard but cherry-picking '
        override='; overriding with standard pipeline.\n'
        if args.denoise:
            warningmsg+=msgstart+stdmsg+'--denoise'+override
        if args.undistort:
            warningmsg+=msgstart+stdmsg+'--eddy'+override
        if args.smooth:
            warningmsg+=msgstart+stdmsg+'--smooth'+override
        # Coerce all of the above to be true
        args.denoise = True
        args.undistort = True
        args.smooth = True
        #--extra options--
        args.mask = True
        args.rpe_none = True
        args.degibbs = True

    # Can't do WMTI if no fit
    if args.nofit:
        stdmsg='--nofit given but '
        if args.wmti:
            warningmsg+=msgstart+stdmsg+'--wmti'+override+'tensor fitting.\n'
            args.nofit = False
        if args.noakc:
            warningmsg+=msgstart+stdmsg+'--noakc'+override+'tensor fitting.\n'
            args.nofit = False
        if args.nooutliers:
            warningmsg+=msgstart+stdmsg+'--nooutliers'
            warningmsg+=override+'tensor fitting.\n'
            args.nofit = False

    # (Extent or Degibbs) and no Denoise
    if not args.denoise:
        stdmsg='No --denoise but '
        if args.extent != '5,5,5':
            warningmsg+=stdmsg+'--extent given; overriding with --denoise\n'
            args.denoise = True
        if args.rician:
            warningmsg+=stdmsg+'--rician given; overriding with --denoise\n'
            args.denoise = True

    # Incompatible eddy args
    if not args.topup and not args.rpe_none and args.undistort:
        errmsg+='Cannot undistort without rpe selection'
    elif args.rpe_pair:
        errmsg+='We are sorry but this feature is unsupported for now.'

    # FWHM given but not smoothing
    if not args.smooth and args.fwhm:
        warningmsg+='No --smooth given but --fwhm given; '
        warningmsg+=' overriding with --smooth\n'
        args.smooth = True

    # Check to make sure CSF mask exists if given
    if args.csfmask:
        if not op.exists(args.csfmask):
            errmsg+='--csfmask file '+args.csfmask+' not found\n'

    # Cannot run --user_mask and --mask at the same time
    if args.user_mask and args.mask:
        errmsg+='Cannot run with both --mask and --user_mask; '
        errmsg+='--mask if you do not have a custom brain mask and ' \
                '--user_mask if you want to supply a mask.'

    # Check to make sure brain mask exists if given
    if args.user_mask:
        if not op.exists(args.user_mask):
            errmsg+='--user_mask file '+args.user_mask+' not found\n'
        # Then check if it's a nifti file
        if not '.nii' in op.splitext(args.user_mask)[-1]:
            errmsg+='User supplied mask if not in NifTi (.nii) format.'

    # Check output directory exists if given
    if args.output:
        if not op.exists(args.output):
            try:
                os.makedirs(args.output, exist_ok=True)
            except:
                errmsg+='Cannot find or create output directory'

    # Check that --fit_constraints can be converted to int array
    fit_constraints = np.fromstring(args.fit_constraints,
                                        dtype=int, sep=',')
    for i in fit_constraints:
        if i < 0 or i > 1:
            errmsg+='Invalid --fit_constraints value, should be 0 or 1\n'
            break

    # --force and --resume given
    if args.resume and args.force:
        errmsg+=msgstart+'--continue and --force\n'

    if args.output:
        if not op.isdir(args.output):
            try:
                os.makedirs(args.output, exist_ok=True)
            except:
                errmsg+=('Output directory does not exist and cannot '
                        'be made.')

    # Print warnings
    if warningmsg is not '':
        print(warningmsg)

    # If things are unsalvageable, point out all errors and quit
    if errmsg is not '':
        raise Exception(errmsg)

    # Begin keeping track of nifti files
    filetable = {'dwi' : DWIFile(init_nii)}
    if not filetable['dwi'].isAcquisition():
        raise Exception('Input dwi does not have .bval/.bvec pair')

    # Begin composing command history
    cmdtable = {'input': mrinfoutil.commandhistory(working_path)}

    # Check to make sure no partial fourier if --degibbs given
    if args.degibbs and args.adv:
        args.degibbs = True
    else:
        if args.degibbs and filetable['dwi'].isPartialFourier():
            print('[WARNING] Given DWI is partial fourier, overriding '
                '--degibbs; no unringing correction will be done to '
                'avoid artifacts.Use the "--adv" flag to run forced '
                'corrections.')
            args.degibbs = False

    if args.rpe_pair:
        filetable['rpe_pair'] = DWIFile(args.rpe_pair)
    if args.rpe_all:
        filetable['rpe_all'] = DWIFile(args.rpe_all)

    if args.topup:
        filetable['topup'] = DWIFile(args.topup)

    #----------------------------------------------------------------------
    # Path Handling
    #----------------------------------------------------------------------
    qcpath = op.join(outpath, 'metrics_qc')
    eddyqcpath = op.join(qcpath, 'eddy')
    fitqcpath = op.join(qcpath, 'fitting')
    metricpath = op.join(outpath, 'metrics')
    if not args.nofit:
        if op.exists(metricpath):
            if args.force:
                shutil.rmtree(metricpath)
            elif not args.resume:
                raise Exception(
                    'Running fitting would cause an overwrite. '
                    'In order to run this please delete the '
                    'files, use --force, use --resume, or '
                    'change output destination.')
        else:
            os.makedirs(metricpath, exist_ok=True)
    if not args.noqc:
        if op.exists(qcpath):
            if args.force:
                shutil.rmtree(qcpath)
            elif not args.resume:
                raise Exception('Running QCing would cause an overwrite. '
                                'In order to run this please delete the '
                                'files, use --force, use --resume, or '
                                'change output destination.')
        else:
            os.makedirs(qcpath, exist_ok=True)
        if op.exists(eddyqcpath) and args.undistort:
            if args.force:
                shutil.rmtree(eddyqcpath)
            elif not args.resume:
                raise Exception('Running dwidenoise would cause an '
                                'overwrite. '
                                'In order to run this please delete the '
                                'files, use --force, or change output '
                                'destination.')
        if op.exists(fitqcpath) and not args.nofit:
            if args.force:
                shutil.rmtree(fitqcpath)
            elif not args.resume:
                raise Exception('Running fitting would cause an '
                                'overwrite. '
                                'In order to run this please delete the '
                                'files, use --force, or change output '
                                'destination.')
        if args.undistort:
            os.makedirs(eddyqcpath, exist_ok=True)
        if not args.nofit:
            os.makedirs(fitqcpath, exist_ok=True)

    # TODO: add non-json RPE support, additional RPE type support

    # Get naming and location information
    dwiname = filetable['dwi'].getName()
    if not args.output:
        outpath = filetable['dwi'].getPath()
    else:
        outpath = args.output
    filetable['outpath'] = outpath

    # Make the pipeline point to dwi as the last file since it's the only one
    # so far
    filetable['HEAD'] = filetable['dwi']

    if args.nthreads and args.verbose:
        print('Using ' + str(args.nthreads) + ' threads.')

    #----------------------------------------------------------------------
    # Run Denoising
    #----------------------------------------------------------------------
    if args.denoise:
        # hardcoding this to be the initial file per dwidenoise
        # recommmendation
        nii_denoised_name = 'd' + filetable['dwi'].getName() + '.nii'
        nii_denoised = op.join(outpath, nii_denoised_name)
        mif_denoised_name = 'dwidn.mif'
        mif_denoised = op.join(outpath, mif_denoised_name)
        # output the noise map even without user permission, space is cheap
        noisemap_name = 'noisemap.nii'
        noisemap = op.join(outpath, noisemap_name)
        # check to see if this already exists
        if not (args.resume and op.exists(denoised) and op.exists(noisemap)):
            # run denoise function
            mrpreproc.denoise(input=working_path,
                              output=mif_denoised,
                              noisemap=True,
                              extent=args.extent,
                              nthreads=args.nthreads,
                              force=False,
                              verbose=args.verbose)
        if args.out_all:
            mrpreproc.miftonii(input=mif_denoised,
                               output=nii_denoised,
                               strides='1,2,3,4',
                               nthreads=args.nthreads,
                               force=args.force,
                               verbose=False)
            # update nifti file tracking
            filetable['denoised'] = DWIFile(nii_denoised)
            filetable['HEAD'] = filetable['denoised']
        filetable['noisemap'] = DWIFile(noisemap)
        # remove old working.mif and replace with new corrected .mif
        os.remove(working_path)
        os.rename(mif_denoised, working_path)
        # update command history
        cmdtable['denoise'] = mrinfoutil.commandhistory(working_path)[-1]
        cmdtable['HEAD'] = cmdtable['denoise']

    #----------------------------------------------------------------------
    # Run Gibbs Unringing
    #----------------------------------------------------------------------
    if args.degibbs:
        # add to HEAD name
        nii_degibbs_name = 'g' + filetable['HEAD'].getName() + '.nii'
        nii_degibbs = op.join(outpath, nii_degibbs_name)
        mif_degibbs_name = 'dwigc.mif'
        mif_degibbs = op.join(outpath, mif_degibbs_name)
        # check to see if this already exists
        if not (args.resume and op.exists(degibbs)):
            # run degibbs function
            mrpreproc.degibbs(input=working_path,
                              output=mif_degibbs,
                              nthreads=args.nthreads,
                              force=False,
                              verbose=args.verbose)
        if args.out_all:
            mrpreproc.miftonii(input=mif_degibbs,
                               output=nii_degibbs,
                               strides='1,2,3,4',
                               nthreads=args.nthreads,
                               force=args.force,
                               verbose=False)
            # update nifti file tracking
            filetable['unrung'] = DWIFile(nii_degibbs)
            filetable['HEAD'] = filetable['unrung']
        # remove old working.mif and replace with new corrected .mif
        os.remove(working_path)
        os.rename(mif_degibbs, working_path)
        # update command history
        cmdtable['degibbs'] = mrinfoutil.commandhistory(working_path)[-1]
        cmdtable['HEAD'] = cmdtable['degibbs']

    #----------------------------------------------------------------------
    # Undistort
    #----------------------------------------------------------------------
    if args.undistort:
        # Add to HEAD name
        nii_undistorted_name = 'u' + filetable['HEAD'].getName() + '.nii'
        nii_undistorted = op.join(outpath, nii_undistorted_name)
        mif_undistorted_name = 'dwiec.mif'
        mif_undistorted = op.join(outpath, mif_undistorted_name)

        # check to see if this already exists
        if not (args.resume and op.exists(undistorted_full)):
            # run undistort function
            mrpreproc.undistort(input=working_path,
                                output=mif_undistorted,
                                rpe='rpe_header',
                                qc=eddyqcpath,
                                nthreads=args.nthreads,
                                force=args.force,
                                verbose=args.verbose)
            if args.out_all:
                mrpreproc.miftonii(input=mif_undistorted,
                                output=nii_undistorted,
                                strides='1,2,3,4',
                                nthreads=args.nthreads,
                                force=args.force,
                                verbose=False)
                # update nifti file tracking
                filetable['undistorted'] = DWIFile(nii_undistorted)
                filetable['HEAD'] = filetable['undistorted']
        # remove old working.mif and replace with new corrected .mif
        os.remove(working_path)
        os.rename(mif_undistorted, working_path)
        # update command history
        cmdtable['undistort'] = mrinfoutil.commandhistory(working_path)
        cmdtable['HEAD'] = cmdtable['undistort']

    #----------------------------------------------------------------------
    # Create Brain Mask
    #----------------------------------------------------------------------
    if (args.maskthr is None) or not (args.maskthr):
        args.maskthr = 0.25
    if args.mask:
        brainmask_name = 'brain_mask.nii'
        brainmask_out = op.join(outpath, brainmask_name)
        mrpreproc.brainmask(input=working_path,
                            output=brainmask_out,
                            thresh=args.maskthr,
                            nthreads=args.nthreads,
                            force=args.force,
                            verbose=args.verbose)
        filetable['mask'] = DWIFile(brainmask_out)
    
    if args.user_mask:
        brainmask_name = 'brain_mask.nii'
        brainmask_out = op.join(outpath, 'brain_mask.nii')
        shutil.copy(args.user_mask, brainmask_out)
        filetable['mask'] = DWIFile(brainmask_out) 

    #----------------------------------------------------------------------
    # Smooth
    #----------------------------------------------------------------------
    if args.smooth:
        # add to HEAD name
        nii_smoothing_name = 's' + filetable['HEAD'].getName() + '.nii'
        nii_smoothing_full = op.join(outpath, nii_smoothing_name)
        mif_smoothing_name = 'dwism.mif'
        mif_smoothing = op.join(outpath, mif_smoothing_name)
        # check to see if this already exists
        if op.exists(nii_smoothing_full):
            if not (args.resume or args.force):
                raise Exception('Running smoothing would cause an overwrite. '
                                'In order to run please delete the files, use '
                                '--force, use --resume, or change output '
                                'destination.')
        if not args.resume:
            if args.fwhm:
                fwhm_i = args.fwhm
            else:
                fwhm_i = 1.25
            mrpreproc.smooth(input=working_path,
                             output=mif_smoothing,
                             fwhm=fwhm_i)
            if args.out_all:
                mrpreproc.miftonii(input=mif_smoothing,
                                   output=nii_smoothing_full,
                                   strides='1,2,3,4',
                                   nthreads=args.nthreads,
                                   force=args.force,
                                   verbose=False)
                filetable['smoothed'] = DWIFile(nii_smoothing_full)
                filetable['HEAD'] = filetable['smoothed']
         # remove old working.mif and replace with new corrected .mif
        os.remove(working_path)
        os.rename(mif_smoothing, working_path)
        # update command history
        cmdtable['smooth'] = mrinfoutil.commandhistory(working_path)[-1]
        cmdtable['HEAD'] = cmdtable['smooth']

    #----------------------------------------------------------------------
    # Rician Noise Correction
    #----------------------------------------------------------------------
    if args.rician:
        # add to HEAD name
        rician_name = 'r' + filetable['HEAD'].getName() + '.nii'
        rician_full = op.join(outpath, rician_name)
        # check to see if this already exists
        if op.exists(rician_full):
            # system call
            if not (args.resume or args.force):
                raise Exception('Running rician correction would cause an '
                                'overwrite. '
                                'In order to run this please delete the '
                                'files, use --force, use --resume, or '
                                'change output destination.')

        if not args.resume:
            rician.rician_img_correct(filetable['HEAD'].getFull(),
                        filetable['noisemap'].getFull(),
                        outpath=rician_full)

        filetable['rician_corrected'] = DWIFile(rician_full)
        filetable['HEAD'] = filetable['rician_corrected']

    #----------------------------------------------------------------------
    # Make preprocessed file
    #----------------------------------------------------------------------
    preprocessed = op.join(outpath, 'preprocessed_dwi.nii')
    mrpreproc.miftonii(input=working_path,
                        output=preprocessed,
                        strides='1,2,3,4',
                        nthreads=args.nthreads,
                        force=args.force,
                        verbose=False)
    filetable['preprocessed'] = DWIFile(preprocessed)
    filetable['HEAD'] = filetable['preprocessed']

    #----------------------------------------------------------------------
    # Compute SNR
    #----------------------------------------------------------------------
    if args.denoise and not args.noqc:
        files = []
        files.append(init_nii)
        files.append(filetable['HEAD'].getFull())
        if not filetable['mask'] is None:
            snr = snrplot.makesnr(dwilist=files,
                                    noisepath=noisemap,
                                    maskpath=filetable['mask'].getFull())
        else:
            snr = snrplot.makesnr(dwilist=files,
                                    noisepath=filetable['noisemap'].getFull(),
                                    maskpath=None)
        snr.makeplot(path=qcpath, smooth=True, smoothfactor=3)

    #----------------------------------------------------------------------
    # Tensor Fitting
    #----------------------------------------------------------------------
    # Define some paths
    if not args.nofit:
        # create dwi fitting object
        if not args.nthreads:
            img = dp.DWI(filetable['HEAD'].getFull())
        else:
            img = dp.DWI(filetable['HEAD'].getFull(), args.nthreads)
        # detect outliers
        if not args.nooutliers:
            if not img.isdki():
                outliers, dt_est = img.irlls(mode='DTI')
            else:
                outliers, dt_est = img.irlls(mode='DKI')
            # write outliers to qc folder
            if not args.noqc:
                outlier_full = op.join(fitqcpath, 'outliers_irlls.nii')
                dp.writeNii(outliers, img.hdr, outlier_full)
            # fit while rejecting outliers
            img.fit(fit_constraints, reject=outliers)
        else:
            # fit without rejecting outliers
            img.fit(fit_constraints)

        if img.isdki() and not args.noakc:
            akc_out = img.akcoutliers()
            img.akccorrect(akc_out)
            dp.writeNii(akc_out,
                        img.hdr,
                        op.join(fitqcpath, 'outliers_akc'))
        md, rd, ad, fa, fe, trace = img.extractDTI()
        dp.writeNii(md, img.hdr, op.join(metricpath, 'md'))
        dp.writeNii(rd, img.hdr, op.join(metricpath, 'rd'))
        dp.writeNii(ad, img.hdr, op.join(metricpath, 'ad'))
        dp.writeNii(fa, img.hdr, op.join(metricpath, 'fa'))
        dp.writeNii(fe, img.hdr, op.join(metricpath, 'fe'))
        if args.median:
            filters.median(
                input=op.join(metricpath, 'md.nii'),
                output=op.join(metricpath, 'md.nii'),
                mask=filetable['mask'].getFull())
            filters.median(
                input=op.join(metricpath, 'rd.nii'),
                output=op.join(metricpath, 'rd.nii'),
                mask=filetable['mask'].getFull())
            filters.median(
                input=op.join(metricpath, 'ad.nii'),
                output=op.join(metricpath, 'ad.nii'),
                mask=filetable['mask'].getFull())
            filters.median(
                input=op.join(metricpath, 'fa.nii'),
                output=op.join(metricpath, 'fa.nii'),
                mask=filetable['mask'].getFull())
            filters.median(
                input=op.join(metricpath, 'fe.nii'),
                output=op.join(metricpath, 'fe.nii'),
                mask=filetable['mask'].getFull())
        if not img.isdki():
            dp.writeNii(trace, img.hdr, op.join(metricpath, 'trace'))
            filters.median(
                input=op.join(metricpath, 'trace.nii'),
                output=op.join(metricpath, 'trace.nii'),
                mask=filetable['mask'].getFull())
        else:
            mk, rk, ak, kfa, mkt, trace = img.extractDKI()
            # naive implementation of writing these variables
            dp.writeNii(mk, img.hdr, op.join(metricpath, 'mk'))
            dp.writeNii(rk, img.hdr, op.join(metricpath, 'rk'))
            dp.writeNii(ak, img.hdr, op.join(metricpath, 'ak'))
            dp.writeNii(kfa, img.hdr, op.join(metricpath, 'kfa'))
            dp.writeNii(mkt, img.hdr, op.join(metricpath, 'mkt'))
            dp.writeNii(trace, img.hdr, op.join(metricpath, 'trace'))
            if args.median:
                filters.median(
                    input=op.join(metricpath, 'mk.nii'),
                    output=op.join(metricpath, 'mk.nii'),
                    mask=filetable['mask'].getFull())
                filters.median(
                    input=op.join(metricpath, 'rk.nii'),
                    output=op.join(metricpath, 'rk.nii'),
                    mask=filetable['mask'].getFull())
                filters.median(
                    input=op.join(metricpath, 'ak.nii'),
                    output=op.join(metricpath, 'ak.nii'),
                    mask=filetable['mask'].getFull())
                filters.median(
                    input=op.join(metricpath, 'kfa.nii'),
                    output=op.join(metricpath, 'kfa.nii'),
                    mask=filetable['mask'].getFull())
                filters.median(
                    input=op.join(metricpath, 'mkt.nii'),
                    output=op.join(metricpath, 'mkt.nii'),
                    mask=filetable['mask'].getFull())
                filters.median(
                    input=op.join(metricpath, 'trace.nii'),
                    output=op.join(metricpath, 'trace.nii'),
                    mask=filetable['mask'].getFull())
            if args.wmti:
                awf, eas_ad, eas_rd, eas_tort, ias_ad, ias_rd, ias_tort = \
                    img.extractWMTI()
                dp.writeNii(awf, img.hdr,
                            op.join(metricpath, 'wmti_awf'))
                dp.writeNii(eas_ad, img.hdr,
                            op.join(metricpath, 'wmti_eas_ad'))
                dp.writeNii(eas_rd, img.hdr,
                            op.join(metricpath, 'wmti_eas_rd'))
                dp.writeNii(eas_tort, img.hdr,
                            op.join(metricpath, 'wmti_eas_tort'))
                dp.writeNii(ias_ad, img.hdr,
                            op.join(metricpath, 'wmti_ias_ad'))
                dp.writeNii(ias_rd, img.hdr,
                            op.join(metricpath, 'wmti_ias_rd'))
                dp.writeNii(ias_tort, img.hdr,
                            op.join(metricpath, 'wmti_ias_tort'))
            # reorder tensor for mrtrix3
            DT, KT = img.tensorReorder(img.tensorType())
            dp.writeNii(DT, img.hdr, op.join(metricpath, 'DT'))
            dp.writeNii(KT, img.hdr, op.join(metricpath, 'KT'))

if __name__ == '__main__':
    main()
