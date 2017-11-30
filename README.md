# electron microscopy data reconstruction pipeline (emdrp)

Tools for volumetric segmentation / reconstruction of nervous tissue from serial electron microscopy data. Potential exists for application to other 3D imaging modalities. See high-level [documentation and introduction](doc/wiki/README.md).

## Publications

> Pallotto M, Watkins PV, Fubara B, Singer JH, Briggman KL. (2015)
> [Extracellular space preservation aids the connectomic analysis of neural circuits.](https://elifesciences.org/articles/08206)
> *Elife.* 2015 Dec 9;4. pii: e08206. doi: 10.7554/eLife.08206. PMID: 26650352

## Installation and Dependencies

python3 is required. anaconda install is recommended, with a few additional [requirements](doc/setup/python3_pip_requirements.txt).

Full usage of the pipeline requires matlab installation with following toolboxes:
- Image Processing Toolbox
- Statistics and Machine Learning Toolbox

The emdrp utilizes [neon](https://github.com/NervanaSystems/neon) as the convnet implementation for machine voxel classification. Sync to the current [supported release](neon3/neon_version.txt), apply a small [patch](neon3/neon.patch) from the path where neon was cloned and install per their instructions (Anaconda install method recommended). Finally install a few additional [requirements](neon3/requirements.txt) in the neon environment.

python C extensions were created for fast performance of some pipeline steps. Build these are built with a simple [Makefile](recon/python/utils/pyCext/Makefile) after modifying the appropriate paths to python and numpy install locations.

Currently the emdrp is more a collection of python, matlab and shell scripts than a toolbox or single install. Until this is remedied, the following need to be added to the respective paths (relative to emdrp clone path):

- PATH
  - `emdrp/recon/python`
- PYTHONPATH
  - `emdrp/recon/python`
  - `emdrp/recon/python/utils`
  - `emdrp/recon/python/utils/pyCext`
- matlab path
  - `emdrp/recon/matlab/hdf5`
  - `emdrp/recon/matlab/knossos`

## Tutorial / Example Workflow

Reset the repository to the [commit]() that works with the example.

Download [datasets](https://elifesciences.org/articles/08206/figures#data-sets) and training and testing data (Figure 3—source data 1 to 4) generated for the [ECS preservation paper](https://elifesciences.org/articles/08206).

Raw data for these test cases is two volumes of size 1024x1024x512 voxels with voxel resolution of 9.8x9.8x25 nm. The data are used for the 3D section of the ECS preservation paper; `M0027_11` is prepared using standard tissue preparation techniques for EM, while `M0007_33` preserves a large percentage of extracellular space.

All scripts for running through this tutorial are located at `pipeline/ECS_tutorial`. Many scripts will require changing paths to the location data files were downloaded to.

### Create data containers

The emdrp uses hdf5 as the container for all data. The first step is to create hdf5 files for the raw EM data using top-level matlab script `top_make_hdf5_from_knossos_raw.m`

Manually annotated training data also needs to be converted to hdf5 using scripts `label_maker*.sh` The emdrp does not support tiff stacks, so the downloaded label data needs to be converted to either nrrd, gipl or raw formats ([fiji](https://fiji.sc/) recommended). Labels can be validated using `label_validator*.sh` scripts. The raw format exports from the emdrp data scripts are typically used in conjunction with [itksnap](http://www.itksnap.org/pmwiki/pmwiki.php) for viewing small volumes.

### Train convnets

To train against all training data with neon, activate the neon environment and run from the emdrp neon3 subdirectory (change paths appropriately):

```
python -u ./emneon.py -e 1 --data_config ~/gits/emdrp/pipeline/ECS_tutorial/EMdata-3class-64x64out-rand-M0007.ini --image_in_size 128 --serialize 800 -s ~/Data/ECS_tutorial/convnet_out/M0007_0.prm -o ~/Data/ECS_tutorial/convnet_out/M0007_0.h5 --model_arch vgg3pool --train_range 100001 112800 --epoch_dstep 5600 4000 2400 --nbebuf 1 -i 0

python -u ./emneon.py -e 1 --data_config ~/gits/emdrp/pipeline/ECS_tutorial/EMdata-3class-64x64out-rand-M0027.ini --image_in_size 128 --serialize 800 -s ~/Data/ECS_tutorial/convnet_out/M0027_0.prm -o ~/Data/ECS_tutorial/convnet_out/M0027_0.h5 --model_arch vgg3pool --train_range 100001 112800 --epoch_dstep 5600 4000 2400 --nbebuf 1 -i 0
```

Typically 4 independent convnets are trained on all training data. However, the agglomeration step of the pipeline trains much better against segmentations created from the test volumes of cross-validated convnets. For a small number of training volumes, a leave-one-volume-out cross-validation, follwed by training the agglomeration with the test volumes, has given the best agglomeration training results.

`emneon.py` contains a handy flag `--chunk-skip-list` for leave-n-out cross validations:

```
python -u ./emneon.py -e 1 --data_config ~/gits/emdrp/pipeline/ECS_tutorial/EMdata-3class-64x64out-rand-M0027.ini --image_in_size 128 --serialize 800 -s ~/Data/ECS_tutorial/convnet_out/M0027_test0_0.prm -o ~/Data/ECS_tutorial/convnet_out/M0027_test0_0.h5 --model_arch vgg3pool --train_range 100001 112800 --epoch_dstep 5600 4000 2400 --nbebuf 1 -i 0 --test_range 200001 200001 --chunk_skip_list 0 --eval 800
```

A total of 28 trained convnets for each dataset should result, 4 each for the six leave-one-volume-out and for training on all volumes.

### Export probabilities

This step exports probability of voxel classification types from each trained convnet. To simplify scripts and preserve some amount of context, the entirety of the volumes is exported for each trained convnet (28 for each dataset). Context outside of the test cube is optionally used by the watershed and agglomeration steps. For example, for each trained convnet:

```
python -u ./emneon.py --data_config ~/gits/emdrp/pipeline/ECS_tutorial/EMdata-3class-64x64out-export-M0007.ini --model_file ~/Data/ECS_tutorial/convnet_out/M0007_0.prm --write_output ~/Data/ECS_tutorial/xfold/M0007_0_probs.h5 --test_range 200001 200256 -i 0
```

### Merge probabilities

Although any number of aggregation of the trained convnets could be used, empirically probability mean, min and max operations have given the best segmentation results. The means are used to generate segmentations in the watershed step and the means and maxes are used as training features in the agglomeration step.

For example, for a single cross-validation:
```
python -u dpMergeProbs.py --srcpath ~/Data/ECS_tutorial/xfold --srcfiles M0007_0_probs.h5 M0007_1_probs.h5 M0007_2_probs.h5 M0007_3_probs.h5 --dim-orderings xyz xyz xyz xyz --outprobs ~/Data/ECS_tutorial/xfold/M0007_probs.h5 --chunk 18 15 3 --size 128 128 128 --types ICS --ops mean min --dpM

python -u dpMergeProbs.py --srcpath ~/Data/ECS_tutorial/xfold --srcfiles M0007_0_probs.h5 M0007_1_probs.h5 M0007_2_probs.h5 M0007_3_probs.h5 --dim-orderings xyz xyz xyz xyz --outprobs ~/Data/ECS_tutorial/xfold/M0007_probs.h5 --chunk 18 15 3 --size 128 128 128 --types MEM ECS --ops mean max --dpM
```

### Watershed

This step which creates the initial segmentations is a custom automatically-seeded watershed algorithm. The algorithm automatically picks seed locations by preserving 3D regions that have fallen below a particular size with increasing thresholds on the mean probabilities. Three segmentations are created:
  1. with_background: voxel identity as predicted with winner-take-all probability from the convnet outputs are preserved
  2. no_adjacencies: supervoxels are flushed out but background is preserved to maintain non-adjacency between components
  3. zero_background: fully watershedded segmentation with no background remaining

For example, for a single cross-validation:
```
python -u dpWatershedTypes.py --probfile ~/Data/ECS_tutorial/xfold/M0007_probs.h5 --chunk 18 15 3 --offset 0 0 0 --size 128 128 128 --outlabels ~/Data/ECS_tutorial/xfold/M0007_supervoxels.h5 --ThrRng 0.5 0.999 0.1 --ThrHi 0.95 0.99 0.995 0.999 0.99925 0.9995 0.99975 
0.9999 0.99995 0.99999 --dpW
```

### Agglomerate

### Skeleton metrics

## Legacy

### Modified cuda-convnets2

Sample run for training modified [cuda-convnet2](https://github.com/akrizhevsky/cuda-convnet2) for EM data:

```
python -u convnet.py --data-path=./emdrp-config/EMdata-3class-16x16out-ebal-huge-all-xyz.ini --save-path=../data --test-range=1-5 --train-range=1-200 --layer-def=./emdrp-config/layers-EM-3class-16x16out.cfg --layer-params=./emdrp-config/layer-params-EM-3class-16x16out.cfg --data-provider=emdata --test-freq=40 --epochs=10 --gpu=0
```
Works with cuda 7.5 and anaconda python2.7 plus additional conda [requirements](doc/setup/python2_conda_requirements.txt).
