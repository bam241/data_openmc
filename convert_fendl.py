#!/usr/bin/env python3

import argparse
from pathlib import Path
import ssl
import subprocess
from urllib.parse import urljoin
from textwrap import dedent
import os
from shutil import rmtree

import openmc.data
from openmc._utils import download

description = """
Download FENDL-3.1d, FENDL-3.1a, FENDL-3.0 or FENDL-2.1  ACE data from the IAEA 
and convert it to a HDF5 library for use with OpenMC.

"""


class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter,
                      argparse.RawDescriptionHelpFormatter):
    pass


parser = argparse.ArgumentParser(
    description=description,
    formatter_class=CustomFormatter
)
parser.add_argument('-d', '--destination', type=Path, default=None,
                    help='Directory to create new library in')
parser.add_argument('--download', action='store_true',
                    help='Download files from IAEA-NDS')
parser.add_argument('--no-download', dest='download', action='store_false',
                    help='Do not download files from IAEA-NDS')
parser.add_argument('--extract', action='store_true',
                    help='Extract tar/zip files')
parser.add_argument('--no-extract', dest='extract', action='store_false',
                    help='Do not extract tar/zip files')
parser.add_argument('--libver', choices=['earliest', 'latest'],
                    default='earliest', help="Output HDF5 versioning. Use "
                    "'earliest' for backwards compatibility or 'latest' for "
                    "performance")
parser.add_argument('-r', '--release', choices=['3.1d', '3.1a', '3.0', '2.1'],
                    default='3.1d', help="The nuclear data library release version. "
                    "The currently supported options are 3.1d, 3.1a, 3.0 and "
                    "2.1")
parser.add_argument('-p', '--particles', choices=['neutron', 'photon'], 
                    nargs='+', default=['neutron', 'photon'], 
                    help="Incident particles to include")
parser.add_argument('--cleanup', action='store_true',
                    help="Remove download directories when data has "
                    "been processed")
parser.add_argument('--no-cleanup', dest='cleanup', action='store_false',
                    help="Do not remove download directories when data has "
                    "been processed")
parser.set_defaults(download=True, extract=True, cleanup=False)
args = parser.parse_args()


# this could be added as an argument to allow different libraries to be downloaded
library_name = 'fendl'
cwd = Path.cwd()  

ace_files_dir = cwd.joinpath('-'.join([library_name, args.release, 'ace']))
endf_files_dir = cwd.joinpath('-'.join([library_name, args.release, 'endf']))

download_path = cwd.joinpath('-'.join([library_name, args.release, 'download']))
# the destination is decided after the release is know to avoid putting the release in a folder with a misleading name
if args.destination is None:
    args.destination = Path('-'.join([library_name, args.release, 'hdf5']))

# =============================================================================
# FUNCTIONS FOR DEALING WITH SPECIAL CASES
#
# Each of these functions should take a Path argument which specifies where 
# the process should take place. The function should return a dictionary 
# containing the following keys:
#   'skip_file' - boolean - indicates whether script should ignore this file 
#                           because it is unusable
#   'err_msg'   - string  - Error message to be printed at the end of the script  
#

# Function to check for k-39 error in FENDL-3.0
def fendl30_k39(file_location):
    os.chdir(file_location)
    err_msg = ''
    
    if 'Inf' in open('19K_039.ace', 'r').read():
        ace_error_warning = """
        WARNING: {} contains 'Inf' values within the XSS array which 
        prevent conversion to a hdf5 file format. This is a known issue
        in FENDL-3.0. {} has not been added to the cross section library 
        """.format(filename, filename.name)
        err_msg = dedent(ace_error_warning)

    os.chdir(cwd)

    return {'skip_file': True, 'err_msg': err_msg}

# Function to split up FENDL-2.1 photon ENDF
def fendl21_endf(file_location):
    os.chdir(file_location)
        
    current_file_str = ''
    last_line_no = 0

    with open('FENDLEP.DAT', 'r') as base_file:
        # When cut the ENDF sections don't have seperate headers. Copy the main
        # header to include in every file
        header_line = base_file.readline()
        current_file_str += header_line
        
        for line in base_file:
            
            current_line_no = int(line.split()[-1])
            
            # Start of a new nuclide
            if current_line_no < last_line_no:
                # Create a temporary file with the data 
                new_endf = open('tmp', 'w+')
                new_endf.write(current_file_str)
                new_endf.seek(0)
                
                # Use the ENDF evaluation method to read the name
                ev = openmc.data.endf.Evaluation(new_endf)
                new_endf.close()
                
                z = ev.target['atomic_number']
                a = ev.target['mass_number']
                new_filename = openmc.data.data.ATOMIC_SYMBOL[z]
                new_filename = new_filename + str(a) + ".endf"

                os.rename('tmp', new_filename)
                
                # Need to add a header line if there isn't one at the start of
                # new nuclide
                if current_line_no == 1:
                    current_file_str = header_line

            current_file_str += line
            last_line_no = current_line_no
    
    os.chdir(cwd)

    return {'skip_file': False, 'err_msg': ''}

# Helper function for checking if there are any special cases defined:
# Function to simplify checking for special cases
def check_special_case(particle_details, script_step):
    if 'special_cases' in particle_details:
        if script_step in particle_details['special_cases']:
            return True
    return False

# This dictionary contains all the unique information about each release. 
# This can be extended to accommodate new releases
release_details = {
    '3.1a': {
        'neutron':{
            'base_url': 'https://www-nds.iaea.org/fendl31/data/neutron/',
            'files': ['fendl31a-neutron-ace.zip'],
            'file_type': 'ace',
            'ace_files': ace_files_dir.glob('*.ace'),
            'compressed_file_size': 384,
            'uncompressed_file_size': 2250
        },
        'photon':{
            'base_url': 'https://www-nds.iaea.org/fendl31/data/atom/',
            'files': ['fendl30-atom-endf.zip'],
            'file_type': 'endf',
            'photo_files': endf_files_dir.joinpath('endf').glob('*.txt'),
            'compressed_file_size': 4,
            'uncompressed_file_size': 12
        }
    },
    '3.1d': {
        'neutron':{
            'base_url': 'https://www-nds.iaea.org/fendl/data/neutron/',
            'files': ['fendl31d-neutron-ace.zip'],
            'file_type': 'ace',
            'ace_files': ace_files_dir.joinpath('fendl31d_ACE').glob('*'),
            'compressed_file_size': 425,
            'uncompressed_file_size': 2290
        },
        'photon':{
            'base_url': 'https://www-nds.iaea.org/fendl/data/atom/',
            'files': ['fendl30-atom-endf.zip'],
            'file_type': 'endf',
            'photo_files': endf_files_dir.joinpath('endf').glob('*.txt'),
            'compressed_file_size': 4,
            'uncompressed_file_size': 12
        }
    },
    '3.0': {
        'neutron':{
            'base_url': 'https://www-nds.iaea.org/fendl30/data/neutron/',
            'files': ['fendl30-neutron-ace.zip'],
            'file_type': 'ace',
            'ace_files': ace_files_dir.joinpath('ace').glob('*.ace'),
            'compressed_file_size': 364,
            'uncompressed_file_size': 2200,
            'special_cases': {
                'process': {'19K_039.ace':fendl30_k39}
            }
        },
        'photon':{
            'base_url': 'https://www-nds.iaea.org/fendl30/data/atom/',
            'files': ['fendl30-atom-endf.zip'],
            'file_type': 'endf',
            'photo_files': endf_files_dir.joinpath('endf').glob('*.txt'),
            'compressed_file_size': 4,
            'uncompressed_file_size': 12
        }
    },
    '2.1': {
        'neutron':{
            'base_url': 'https://www-nds.iaea.org/fendl21/fendl21mc/',
            'files': ['H001mc.zip',  'H002mc.zip',  'H003mc.zip',  'He003mc.zip', 
                    'He004mc.zip', 'Li006mc.zip', 'Li007mc.zip', 'Be009mc.zip',
                    'B010mc.zip',  'B011mc.zip',  'C012mc.zip',  'N014mc.zip', 
                    'N015mc.zip',  'O016mc.zip',  'F019mc.zip',  'Na023mc.zip',
                    'Mg000mc.zip', 'Al027mc.zip', 'Si028mc.zip', 'Si029mc.zip',
                    'Si030mc.zip', 'P031mc.zip',  'S000mc.zip',  'Cl035mc.zip', 
                    'Cl037mc.zip', 'K000mc.zip',  'Ca000mc.zip', 'Ti046mc.zip', 
                    'Ti047mc.zip', 'Ti048mc.zip', 'Ti049mc.zip', 'Ti050mc.zip', 
                    'V000mc.zip',  'Cr050mc.zip', 'Cr052mc.zip', 'Cr053mc.zip', 
                    'Cr054mc.zip', 'Mn055mc.zip', 'Fe054mc.zip', 'Fe056mc.zip', 
                    'Fe057mc.zip', 'Fe058mc.zip', 'Co059mc.zip', 'Ni058mc.zip', 
                    'Ni060mc.zip', 'Ni061mc.zip', 'Ni062mc.zip', 'Ni064mc.zip', 
                    'Cu063mc.zip', 'Cu065mc.zip', 'Ga000mc.zip', 'Zr000mc.zip', 
                    'Nb093mc.zip', 'Mo092mc.zip', 'Mo094mc.zip', 'Mo095mc.zip', 
                    'Mo096mc.zip', 'Mo097mc.zip', 'Mo098mc.zip', 'Mo100mc.zip', 
                    'Sn000mc.zip', 'Ta181mc.zip', 'W182mc.zip',  'W183mc.zip',  
                    'W184mc.zip',  'W186mc.zip',  'Au197mc.zip', 'Pb206mc.zip', 
                    'Pb207mc.zip', 'Pb208mc.zip', 'Bi209mc.zip'],
            'file_type': 'ace',
            'ace_files': ace_files_dir.glob('*.ace'),
            'compressed_file_size': 100,
            'uncompressed_file_size': 600
        },
        'photon':{
            'base_url': 'https://www-nds.iaea.org/fendl21/fendl21e/',
            'files': ['FENDLEP.zip'],
            'file_type': 'endf',
            'photo_files': endf_files_dir.glob('*.endf'),
            'compressed_file_size': 2,
            'uncompressed_file_size': 5,
            'special_cases': {
                'extract': {'FENDLEP.DAT':fendl21_endf}
            }
        }
    }
}

# Function to split an ENDF file made up of multiple evaluations into seperate
# files, each with one evaluation.
def split_endf(filename):
    """Divides a file which contains multiple ENDF entries into individual 
       ENDF files.

        Parameters 
        ----------
        filename : str
            Path to file with multiple ENDF records
        
        Returns
        -------
        list
            A list of the names of files created
    """
        
    with open(str(filename), 'r') as fh:
  
        current_file_str = ''
        last_line_no = 0
        created_files = []

        # When cut the ENDF sections don't have seperate headers. Copy the main
        # header to include in every file
        header_line = fh.readline()
        current_file_str += header_line

        for line in fh:
            
            current_line_no = int(line.split()[-1])
            
            # Start of a new nuclide
            if current_line_no < last_line_no:
                # Create a temporary file with the data 
                new_endf = open('tmp', 'w+')
                new_endf.write(current_file_str)
                new_endf.seek(0)

                # Read the name of the new nuclide
                items = openmc.data.endf.get_head_record(fh)
                Z, A = divmod(items[0], 1000)

                new_endf.close()
                
                new_filename = openmc.data.data.ATOMIC_SYMBOL[Z]
                new_filename = new_filename + str(A) + ".endf"

                os.rename('tmp', new_filename)
                created_files.append(new_filename)
                
                # Reset for the next ENDF file
                if current_line_no == 1:
                    # Prepend a header if the next ENDF doesn't have one
                    current_file_str = header_line

            current_file_str += line
            last_line_no = current_line_no
        
        return created_files

compressed_file_size, uncompressed_file_size = 0, 0
for p in ('neutron', 'photon'):
    if p in args.particles:
        compressed_file_size += release_details[args.release][p]['compressed_file_size']
        uncompressed_file_size += release_details[args.release][p]['uncompressed_file_size']

download_warning = """
WARNING: This script will download {} MB of data.
Extracting and processing the data requires {} MB of additional free disk space.
""".format(compressed_file_size, uncompressed_file_size)

# Warnings to be printed at the end of the script.
output_warnings = [] 

# ==============================================================================
# DOWNLOAD FILES FROM IAEA SITE

if args.download:
    print(download_warning)

    for particle in args.particles:
        # Create a directory to hold the downloads
        particle_download_path = download_path / particle
        particle_download_path.mkdir(parents = True, exist_ok=True) 
        os.chdir(particle_download_path)

        particle_details = release_details[args.release][particle]
        for f in particle_details['files']:
            download(urljoin(particle_details['base_url'], f),
                    as_browser=True, context=ssl._create_unverified_context())
    
    os.chdir(cwd)



# ==============================================================================
# EXTRACT FILES FROM ZIP
if args.extract:
    for particle in args.particles:
        os.chdir(download_path / particle)

        particle_details = release_details[args.release][particle]
        if particle_details['file_type'] == "ace":
            extraction_dir = ace_files_dir
        elif particle_details['file_type'] == "endf":
            extraction_dir = endf_files_dir

        for f in particle_details['files']:
            # Extract files, the fendl release was compressed using type 9 zip format
            # unfortunatly which is incompatible with the standard python zipfile library
            # therefore the following system command is used
            subprocess.call(['unzip', '-o', f, '-d', extraction_dir])
            if args.cleanup and Path(f).exists():
                Path(f).unlink()

        if check_special_case(particle_details, 'extract'):
            special_cases = particle_details['special_cases']['extract']
            for case in special_cases:
                ret = special_cases[case](extraction_dir)
                output_warnings.append(ret['err_msg'])
        
    os.chdir(cwd)
    
    if args.cleanup and download_path.exists():
        rmtree(download_path)


# ==============================================================================
# GENERATE HDF5 LIBRARY

library = openmc.data.DataLibrary()

for particle in args.particles:
    # Create output directories if it doesn't exist
    particle_destination = args.destination / particle
    particle_destination.mkdir(parents=True, exist_ok=True)

    particle_details = release_details[args.release][particle]

    if particle == 'neutron':
        # Get a list of all ACE files, excluding files ending with _ which are 
        # old incorrect files kept in the release for backwards compatability
        neutron_files = [
            f
            for f in particle_details['ace_files']
            if not f.name.endswith('_') and not f.name.endswith('.xsd')
        ]
        for filename in sorted(neutron_files):
            
            if check_special_case(particle_details, 'process'):
                special_cases = particle_details['special_cases']['extract']
                if filename.name in special_cases:
                    ret = special_cases[filename.name](ace_files_dir)
                    output_warnings.append(ret['err_msg'])
                    if ret['skip_file']:
                        continue
            
            print(f'Converting: {filename}')
            data = openmc.data.IncidentNeutron.from_ace(filename)

            # Export HDF5 file
            h5_file = particle_destination / f'{data.name}.h5'
            print(f'Writing {h5_file}...')
            data.export_to_hdf5(h5_file, 'w', libver=args.libver)

            # Register with library
            library.register_file(h5_file)

        # Remove the ace files if required    
        if args.cleanup and ace_files_dir.exists():
            rmtree(ace_files_dir)

    elif particle == 'photon':
        for photo_path in sorted(particle_details['photo_files']):
            print(f'Converting: {photo_path}')
            data = openmc.data.IncidentPhoton.from_endf(photo_path)

            # Export HDF5 file
            h5_file = particle_destination / f'{data.name}.h5'
            print(f'Writing {h5_file}...')
            data.export_to_hdf5(h5_file, 'w', libver=args.libver)

            # Register with library
            library.register_file(h5_file)
        
        # Remove the ENDF files if required 
        if args.cleanup and endf_files_dir.exists():
            rmtree(endf_files_dir)

# Write cross_sections.xml
print('Writing ', args.destination / 'cross_sections.xml')
library.export_to_xml(args.destination / 'cross_sections.xml')

# Print any warnings
for warning in output_warnings:
    print(warning)

