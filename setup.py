"""Setup script for odin_workshop python package."""

import sys
from setuptools import setup, find_packages
import versioneer

with open('requirements.txt') as f:
    required = f.read().splitlines()

setup(name='spitest',
      version=versioneer.get_version(),
      cmdclass=versioneer.get_cmdclass(),
      description='Test for SPI and structure',
      url='https://github.com/TeraMike/SPI-test',
      author='Michael Shearwood',
      author_email='michael.shearwood@stfc.ac.uk',
      packages=find_packages(),
      install_requires=required,
      zip_safe=False,
)
