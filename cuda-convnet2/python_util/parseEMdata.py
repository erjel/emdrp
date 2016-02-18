# The MIT License (MIT)
# 
# Copyright (c) 2016 Paul Watkins, National Institutes of Health / NINDS
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Generate EM data for cuda-convnet2. Does all the work for the EMDataProvider.
# Also added "unpackager" routine for recreating output probabilities from feature writer for convnet prob layer.
# New method adopted from datapkgEMnew.py that generated data for EMGPUInd provider in cuda-convnet-EM-alpha.
# Data is parsed out of hdf5 files for raw EM data and for labels.
# Each batch is then generated on-demand (in parallel with training previous batch).
#     This process should not take more than a few seconds per batch (use verbose flag to print run times).
#     The process must be shorter than the GPU batch time, so that the parsing is not the speed bottleneck.
#     New feature allows for batches from multiple areas of the dataset, each area (given by size parameter and list of
#         "chunk" ranges) is loaded once per batch.
# 
# The hdf5 inputs can be C-order or F-order (specified in ini). hdf5 outputs always written in F-order.

import h5py
import numpy as np
from operator import add, sub
import time
import numpy.random as nr
import os, sys
from configobj import ConfigObj, flatten_errors
from validate import Validator, ValidateError
import random
import pickle as myPickle
import StringIO as myStringIO

from scipy import linalg
import scipy.ndimage.filters as filters
from scipy import interpolate
from threading import Thread

# no exception for plotting so this can still work from command line only (plotting is only for standalone validation)
try: 
    from matplotlib import pylab as pl
    import matplotlib as plt
except: pass 


'''
# for easy parallelization of raw em standard processing (upsample, median filter, downsample)
class EMStandardFilterThread(Thread):
    def __init__(self, dp, data, reScale):
        Thread.__init__(self)
        self.dp = dp
        self.data = data
        self.reScale = reScale
        
    def run(self):
        EMDataParser.emStandardFilterProc(self.dp, self.data, self.reScale)
'''


class EMDataParser():

    # Constants

    # Numpy type to be used for cube subscripts. Throwback to parsing cube on GPU memory, but kept type for numpy.
    # Very unlikely to need more than 16 bits per index (also supports loads in imagej from hdf5).
    cubeSubType = np.uint16; cubeSubLim = 65536

    # optional output file names / dataset names
    INFO_FILE = 'batch.info'
    OUTPUT_H5_CVIN = 'batch_input_data.h5'
    OUTPUT_H5_CVOUT = 'batch_output_data.h5'
    PRIOR_DATASET = 'prior_train'
    
    # Where a batch is within these ranges allows for different types of batches to be selected:
    # 1 to FIRST_RAND_NOLOOKUP_BATCH-1 are randomized examples from rand cube based on lookup table
    # FIRST_RAND_NOLOOKUP_BATCH - FIRST_TILED_BATCH-1 are randomized examples from rand cube without lookup table
    # FIRST_TILED_BATCH - (batch with max test chunk / zslice) are tiled examples from the rand then test cubes
    FIRST_RAND_NOLOOKUP_BATCH = 100001
    FIRST_TILED_BATCH = 200001 

    # for preprocessing
    PDTYPE = np.double
    NUM_FILTER_THREADS = 4

    # others    
    NAUGS = 16              # total number of simple augmentations (including reflections in z, 8 for xy augs only)
    HDF5_CLVL = 5           # compression level in hdf5
    
    def __init__(self, cfg_file, write_outputs=False, init_load_path='', save_name=None, append_features=False,
            chunk_skip_list=[], dim_ordering=''):
        self.cfg_file = cfg_file
        self.write_outputs = write_outputs; self.save_name = save_name; self.append_features = append_features

        print 'EMDataParser: config file ''%s''' % cfg_file
        # retrieve / save options from ini files, see definitions parseEMdataspec.ini
        opts = EMDataParser.get_options(cfg_file)
        for k, v in opts.items(): 
            if type(v) is list and k not in ['chunk_skip_list','aug_datasets']: 
                if len(v)==1:
                    setattr(self,k,v[0])  # save single element lists as first element
                elif len(v)>0 and type(v[0]) is int:   # convert the sizes and offsets to numpy arrays
                    setattr(self,k,np.array(v,dtype=np.int32))
                else:
                    setattr(self,k,v)   # store other list types as usual (floats, empties)
            else:
                setattr(self,k,v)

        # Options / Inits

        # Previously had these as constants, but moved label data type to ini file and special labels are defined 
        #   depending on the data type.
        self.cubeLblType = eval('np.' + self.cubeLblTypeStr)
        self.EMPTY_LABEL = np.iinfo(self.cubeLblType).max
        self.ECS_LABEL   = np.iinfo(self.cubeLblType).max-1   # xxx - likely not using this, but keep here for now
        self.EMPTY_PROB  = -1.0

        # the manner in which the zreslice is defined, define sort from data -> re-order and from re-order -> data.
        # only 3 options because data can be automatically augmented to transpose the first two dims (in each "z-slice")
        # these orders were chosen because the "unsort" is the same as the "sort" indexing, so re-order->data not needed
        if dim_ordering: self.dim_ordering = dim_ordering   # allow command line override
        if self.dim_ordering=='xyz':
            self.zreslice_dim_ordering = [0,1,2]
        elif self.dim_ordering=='xzy':
            self.zreslice_dim_ordering = [0,2,1]
        elif self.dim_ordering=='zyx':
            self.zreslice_dim_ordering = [2,1,0]
        else:
            assert(False)   # bad dim_ordering parameter given

        # immediately re-order any arguments that need it because of reslice. this prevents from having to do this on 
        #   command line, which ended up being annoying.
        # originally reading the hdf5 was done using arguments that were re-ordered on command line, so those needed
        #   during read are un-re-ordered (back to normal order) in readCubeToBuffers.
        # considered changing this, but calculations are more intuitive after the re-order, so left for the sizes
        self.size_rand = self.size_rand[self.zreslice_dim_ordering]
        self.read_size = self.read_size[self.zreslice_dim_ordering]
        self.read_border = self.read_border[self.zreslice_dim_ordering]
        if self.nz_tiled < 0: self.nz_tiled = self.size_rand[2]

        # initialize for "chunkrange" or "chunklist" mode if these parameters are not empty
        self.use_chunk_list = (len(self.chunk_range_beg) > 0); self.use_chunk_range = False
        if self.use_chunk_list:
            assert( self.nz_tiled == 0 ) # do not define tiled cube for chunklist mode
            self.chunk_range_beg = self.chunk_range_beg.reshape(-1,3); self.chunk_list_index = -1
            self.nchunk_list = self.chunk_range_beg.shape[0]
            if len(self.chunk_range_end) > 0:
                # "chunkrange" mode, chunks are selected based on defined beginning and end of ranges in X,Y,Z
                # range is open ended (python-style, end is not included in range)
                self.chunk_range_end = self.chunk_range_end.reshape(-1,3);
                assert( self.chunk_range_end.shape[0] == self.nchunk_list )
                self.chunk_range_index = -1; self.use_chunk_range = True
                self.chunk_range_rng = self.chunk_range_end - self.chunk_range_beg
                assert( (self.chunk_range_rng >= 0).all() )     # some bad ranges
                self.chunk_range_size = self.chunk_range_rng.prod(axis=1)
                self.chunk_range_cumsize = np.concatenate((np.zeros((1,),dtype=self.chunk_range_size.dtype), 
                    self.chunk_range_size.cumsum()))
                self.chunk_range_nchunks = self.chunk_range_cumsize[-1]
                self.nchunks = self.chunk_range_nchunks
            else:
                # regular chunklist mode, chunk_range_beg just contains the list of the chunks to use
                self.nchunks = self.nchunk_list

            # this code is shared by defining max number of chunks depending on chunk list or chunk range mode.
            # default for the chunk_range_rand is the max number of chunks.
            if self.chunk_range_rand < 0: self.chunk_range_rand = self.nchunks
            assert( self.chunk_range_rand <= self.nchunks )

            # offsets are either per chunk or per range, depending on above mode (whether chunk_range_end empty or not)
            if len(self.offset_list) > 0:
                self.offset_list = self.offset_list.reshape(-1,3)
                assert( self.offset_list.shape[0] == self.nchunk_list )
            else:
                self.offset_list = np.zeros_like(self.chunk_range_beg)
                
            # create a list for random chunks in chunk_range_rand based on the chunk_skip_list, if provided.
            # let command line override definition in ini file.
            if len(chunk_skip_list) > 0: self.chunk_skip_list = chunk_skip_list
            if len(self.chunk_skip_list) > 0:
                mask = np.zeros((self.nchunks,), dtype=np.bool)
                mask[:self.chunk_range_rand] = 1
                mask[np.array(self.chunk_skip_list, dtype=np.int64)] = 0
                self.chunk_rand_list = np.nonzero(mask)[0].tolist()
                self.chunk_range_rand = len(self.chunk_rand_list)
                self.chunk_tiled_list = np.nonzero(np.logical_not(mask))[0].tolist()
            else:
                self.chunk_rand_list = range(self.chunk_range_rand)
                self.chunk_tiled_list = range(self.chunk_range_rand,self.nchunks)

            # the tiled chunks default to all the chunks. 
            # the chunk_skip_is_test parameter makes the tiled chunks all the ones that are not rand chunks.
            if not self.chunk_skip_is_test: self.chunk_tiled_list = range(self.nchunks)

            # xxx - not an easy way not to call initBatches in the beginning without breaking everything,
            #   so just load the first chunk, if first batch is an incremental chunk rand batch, it should not reload
            self.chunk_rand = self.chunk_range_beg[0,:]; self.offset_rand = self.offset_list[0,:]

            # print out info for chunklist / chunkrange modes so that input data is logged
            print ('EMDataParser: Chunk mode with %d ' % self.nchunk_list) + \
                ('ranges' if self.use_chunk_range else 'chunks') + \
                (' of size %d %d %d:' % tuple(self.size_rand[self.zreslice_dim_ordering].tolist()))
            fh = myStringIO.StringIO()
            if self.use_chunk_range:
                np.savetxt(fh, np.concatenate((np.arange(self.nchunk_list).reshape((self.nchunk_list,1)), 
                    self.chunk_range_beg, self.chunk_range_end, self.chunk_range_size.reshape((self.nchunk_list,1)), 
                    self.offset_list), axis=1), 
                    fmt='\t(%d) range %d %d %d to %d %d %d (%d chunks), offset %d %d %d', 
                    delimiter='', newline='\n', header='', footer='', comments='')            
            else:
                np.savetxt(fh, np.concatenate((np.arange(self.nchunk_list).reshape((self.nchunk_list,1)), 
                    self.chunk_range_beg, self.offset_list), axis=1), fmt='\t(%d) chunk %d %d %d, offset %d %d %d', 
                    delimiter='', newline='\n', header='', footer='', comments='')            
            cstr = fh.getvalue(); fh.close(); print cstr
            print '\tchunk_list_rand %d, chunk_range_rand %d' % (self.chunk_list_rand, self.chunk_range_rand)
            print '\tchunk_skip_list: ' + str(self.chunk_skip_list)

        # need these for appending features in chunklist mode, otherwise they do nothing
        #self.last_chunk_rand = self.chunk_rand; self.last_offset_rand = self.offset_rand
        # need special case for first chunk incase there is no next ever loaded (if only one chunk written)
        self.last_chunk_rand = None; self.last_offset_rand = None

        # some calculated values based on constants and input arguments for tiled batches
        
        # few checks here on inputs if not checkable by ini specifications
        assert( self.nzslices == 1 or self.nzslices == 3 ) # xxx - 1 or 3 (or multiple of 4?) only supported by convnet
        # to be certain things don't get off with augmentations, in and out size need to both be even or odd
        assert( (self.image_size % 2) == (self.image_out_size % 2) )
        assert( self.independent_labels or self.image_out_size == 1 ) # need independent labels for multiple pixels out
        assert( not self.no_labels or self.no_label_lookup )    # must have no_label_lookup in no_labels mode

        # number of cases per batch should be kept lower than number of rand streams in convnet (128*128 = 16384)
        self.num_cases_per_batch = self.tile_size.prod()
        self.shape_per_batch = self.tile_size.copy(); self.shape_per_batch[0:2] *= self.image_out_size
        # kept this here so I'm not tempted to do it again. tile_size is NOT re-ordered, too confusing that way
        #self.shape_per_batch[self.zreslice_dim_ordering[0:2]] *= self.image_out_size
        if self.verbose: print "size rand %d %d %d, shape per batch %d %d %d" % \
            tuple(np.concatenate((self.size_rand, self.shape_per_batch)).tolist())
        self.size_total = self.size_rand.copy(); self.size_total[2] += self.nz_tiled
        assert( ((self.size_total % self.shape_per_batch) == 0).all() )
        self.tiles_per_zslice = self.size_rand / self.shape_per_batch
        self.pixels_per_image = self.nzslices*self.image_size**2;
        self.pixels_per_out_image = self.image_out_size**2;
        # need data for slices above and below labels if nzslices > 1, keep data and labels aligned
        self.nrand_zslice = self.size_rand[2] + self.nzslices - 1
        self.ntiled_zslice = self.nz_tiled + self.nzslices - 1
        self.ntotal_zslice = self.nrand_zslice + self.ntiled_zslice
        # x/y dims on tiled zslices are the same size as for selecting rand indices
        self.size_tiled = np.array((self.size_rand[0], self.size_rand[1], self.nz_tiled), dtype=np.int32)
        if self.image_size % 2 == 1:
            self.data_slice_size = (self.size_rand[0] + self.image_size - 1, self.size_rand[1] + self.image_size - 1,
                self.ntotal_zslice);
        else:
            self.data_slice_size = (self.size_rand[0] + self.image_size, self.size_rand[1] + self.image_size,
                self.ntotal_zslice);
        self.labels_slice_size = (self.size_rand[0], self.size_rand[1], self.ntotal_zslice)
        # xxx - not sure why I didn't include the nzslice into the labels offset, throwback to old provider only?
        self.labels_offset = (self.image_size/2, self.image_size/2, 0)
        
        # these were previously hidden passing to GPU provider, introduce new variables
        self.batches_per_zslice = self.tiles_per_zslice[0] * self.tiles_per_zslice[1]
        self.num_inds_tiled = self.num_cases_per_batch * self.batches_per_zslice
        self.zslices_per_batch = self.tile_size[2]
        # there can either be multiple batches per zslice or multiple zslices per batch
        assert( (self.batches_per_zslice == 1 and self.zslices_per_batch >= 1) or \
            (self.batches_per_zslice > 1 and self.zslices_per_batch == 1) );
        self.batches_per_rand_cube = self.nrand_zslice * self.batches_per_zslice / self.zslices_per_batch
        
        self.getLabelMap()      # setup for label types, need before any other inits referencing labels
        self.segmented_labels_slice_size = tuple(map(add,self.labels_slice_size,
            (2*self.segmented_labels_border).tolist()))
        #assert( self.segmented_labels_border[0] == self.segmented_labels_border[1] and \
        #    self.segmented_labels_border[2] == 0 ); # had to add this for use in GPU labels slicing
        
        # because of the affinity graphs, segmented labels can have a border around labels.
        # plot and save this way for validation.
        self.seg_out_size = self.image_out_size + 2*self.segmented_labels_border[0]
        self.pixels_per_seg_out = self.seg_out_size**2
        
        # optional border around "read-size"
        # this is used so that densely labeled front-end cubes do not require label merging, instead just don't select
        #   training examples from around some border of each of these "read-size" cubes.
        if (self.read_size < 0).any():
            self.read_size = self.size_rand
        assert( ((self.size_rand % self.read_size) == 0).all() )

        # use the read border to prevent randomized image out patches from going outside of rand size
        self.read_border[0:2] += self.image_out_size/2;
        
        # additionally image out patches can be offset from selected pixel (label lookup), so remove this also 
        #   if label lookup is enabled. this randomized offset is used to reduce output patch correlations.
        if not self.no_label_lookup:
            self.read_border[0:2] += self.image_out_offset/2
            self.pixels_per_out_offset = self.image_out_offset**2

        # default for label train prior probabilities is uniform for all label types
        if type(self.label_priors) is list: 
            assert( len(self.label_priors) == self.nlabels )
            self.label_priors = np.array(self.label_priors,dtype=np.double)
        else: 
            self.label_priors = 1.0/self.nlabels * np.ones((self.nlabels,),dtype=np.double)
        # this is incase the prior has to be forced to zero because of missing labels after border is removed.
        # reload the original during makeRandLabelLookup for chunk list or chunk range modes.
        self.initial_label_priors = self.label_priors.copy()

        # these are used for making the output probability cubes
        # xxx - the tiling procedure is confusing, see comments on this in makeTiledIndices
        self.output_size = list(self.labels_slice_size)
        self.output_size[0] /= self.image_out_size; self.output_size[1] /= self.image_out_size

        '''
        # inits for preprocessing
        
        # check for file to load whitening matrix from
        if self.whiten_load:
            if not os.path.isfile(self.whiten_load):
                self.whiten_load = os.path.join(init_load_path,self.whiten_load)
                if not os.path.isfile(self.whiten_load) and not os.path.isdir(self.whiten_load):
                    self.whiten_load = init_load_path
        else:
            self.whiten_load = init_load_path
        self.do_whiten_load = os.path.isfile(self.whiten_load)

        assert( all([d <= 1 for d in self.em_standard_idelta]) )
        # optimization for integer multiple interpolations
        self.em_standard_interpn = [int(round(1/d)) for d in self.em_standard_idelta];
        self.em_standard_iinterp = all([np.equal(np.mod(1/d, 1), 0) for d in self.em_standard_idelta])
        '''
        
        # augmented data cubes can be presented in parallel with raw EM data
        self.naug_data = len(self.aug_datasets)
        self.aug_data = [None]*self.naug_data
        self.aug_actual_mean = np.zeros((self.naug_data,),dtype=np.double)
        self.aug_actual_std = np.zeros((self.naug_data,),dtype=np.double)
        assert( len(self.aug_mean) >= self.naug_data )
        assert( len(self.aug_std) >= self.naug_data )
        
        # print out all initialized variables in verbose mode
        if self.verbose: 
            tmp = vars(self); #tmp['indep_label_names_out'] = 'removed from print for brevity'
            print 'EMDataParser, vars after init:\n'; print tmp
        
        # other inits
        self.label_lookup_lens = self.nlabels*[0]
        self.rand_priors = self.nlabels * [0.0]; self.tiled_priors = self.nlabels * [0.0]

        # the prior to use to reweight exported probabilities.
        # training prior is calculated on-the-fly by summing output label targets.
        # this is done because for multiple outputs and label selection schemes different from the labels
        #   label_priors does not give a good indication of the labels that actual end up getting selected
        # xxx - might be better to rename label_priors and other priors since they are not acutal used as priors
        self.prior_test = np.array(self.prior_test, dtype=np.double)

    def initBatches(self, silent=False):
        # turns off printouts during runs if in chunklist or chunkrange mode
        self.silent = silent
        
        if self.write_outputs:
            if not os.path.exists(self.outpath): os.makedirs(self.outpath)
            outfile = open(os.path.join(self.outpath, self.INFO_FILE), 'w'); outfile.close(); # truncate
            outfile = h5py.File(os.path.join(self.outpath, self.OUTPUT_H5_CVIN), 'w'); outfile.close(); # truncate
            
        self.readCubeToBuffers()
        self.setupLabels()
        if not self.no_label_lookup: self.makeRandLabelLookup()
        if self.write_outputs: self.enumerateTiledLabels()
        # for chunklist or chunkrange modes, the tiled indices do not change, so no need to regenerate them
        # xxx - use hasattr for this in a number of spots, maybe change to a single boolean for readability?
        if not hasattr(self, 'inds_tiled'): self.makeTiledIndices()
        if self.write_outputs: self.writeH5Cubes()
        self.makeBatchMeta()
        #self.initPreprocessData()   # this needs to be done last, init for per batch preprocessing
        
        self.silent = False

    def makeRandLabelLookup(self):
        if not self.silent: print 'EMDataParser: Creating rand label lookup for specified zslices'
        self.label_priors[:] = self.initial_label_priors     # incase prior was modified below in chunk mode
        max_total_voxels = self.size_rand.prod() # for heuristic below - xxx - rethink this, or add param?
        total_voxels = 0
        self.inds_label_lookup = self.nlabels*[None]
        for i in range(self.nlabels):
            inds = np.transpose(np.nonzero(self.labels_cube[:,:,0:self.nrand_zslice] == i))
            if inds.shape[0] > 0:
                # don't select from end slots for multiple zslices per case
                inds = inds[np.logical_and(inds[:,2] >= self.nzslices/2, 
                    inds[:,2] < self.nrand_zslice-self.nzslices/2),:]
                inds = self.rand_inds_remove_border(inds)
                
                # if after removing borders a label is missing, do not hard error, just force the prior to zero
                # xxx - heuristic for removing labels with very few members, 1/32^3, use param?
                if inds.shape[0] == 0 or float(inds.shape[0])/max_total_voxels < 3.0517578125e-05:
                    if not self.silent: print 'EMDataParser: no voxels with label %d forcing prior to zero' % i
                    # redistribute current prior amongst remaining nonzero priors
                    prior = self.label_priors[i]; self.label_priors[i] = 0
                    pinds = np.arange(self.nlabels)[self.label_priors > 0]
                    self.label_priors[pinds] += prior/pinds.size
                    #assert( self.label_priors.sum() - 1.0 < 1e-5 )
                
                inds += self.labels_offset
                assert( np.logical_and(inds >= 0, inds < self.cubeSubLim).all() )
                self.label_lookup_lens[i] = inds.shape[0]; total_voxels += inds.shape[0]
                self.inds_label_lookup[i] = inds.astype(self.cubeSubType, order='C')
                if self.write_outputs:
                    outfile = h5py.File(os.path.join(self.outpath, self.OUTPUT_H5_CVIN), 'a'); 
                    outfile.create_dataset('inds_label_lookup_%d' % i,data=inds,compression='gzip', 
                        compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
                    outfile.close();
            else:
                # if a label is missing, must specify label priors on command line to handle this.
                # xxx - maybe do the same as above for this, just remove and redistribute this prior?
                if not self.silent: print 'EMDataParser: no voxels with label %d' % i
                assert(self.label_priors[i] == 0) # prior must be zero if label is missing

        assert(total_voxels > 0);            
        self.rand_priors = [float(x)/total_voxels for x in self.label_lookup_lens]
        if self.write_outputs:
            outfile = open(os.path.join(self.outpath, self.INFO_FILE), 'a')
            outfile.write('\nTotal voxels included for random batches %u\n' % (total_voxels,))
            for i in range(self.nlabels):
                outfile.write('label %d %s percentage of voxels = %.8f , count = %d, use prior %.8f\n' %\
                    (i,self.label_names[i],self.rand_priors[i],self.label_lookup_lens[i],self.label_priors[i]))
            outfile.write('Sum percentage of allowable rand voxels = %.3f\n' % sum(self.rand_priors))
            outfile.close(); 

    # border is used to not select training examples from areas of "read-size" cubes between which the labels are
    #   potentially not consistent (because they were densely labeled separately).
    def rand_inds_remove_border(self, inds):
        nread_cubes = (self.size_rand / self.read_size)
        for d in range(3):
            bmin = self.read_border[d]; inds = inds[inds[:,d] >= bmin,:]
            for i in range(1,nread_cubes[d]):
                bmin = i*self.read_size[d] - self.read_border[d]; bmax = i*self.read_size[d] + self.read_border[d];
                inds = inds[np.logical_or(inds[:,d] < bmin, inds[:,d] >= bmax),:]
            bmax = self.size_rand[d] - self.read_border[d]; inds = inds[inds[:,d] < bmax,:]
        return inds

    def enumerateTiledLabels(self):
        if not self.silent: print 'EMDataParser: Enumerating tiled labels (for prior probabilities)'
        #total_voxels = self.size_tiled.prod() # because of potential index selects or missing labels, sum instead
        tiled_count = self.nlabels * [0]; total_voxels = 0 
        for i in range(self.nlabels):
            inds = np.transpose(np.nonzero(self.labels_cube[:,:,self.nrand_zslice:self.ntotal_zslice] == i))
            if inds.shape[0] > 0:
                # don't select from end slots for multiple zslices per case
                inds = inds[np.logical_and(inds[:,2] >= self.nzslices/2, 
                    inds[:,2] < self.ntiled_zslice-self.nzslices/2),:]
            tiled_count[i] += inds.shape[0]; total_voxels += inds.shape[0]
        
        outfile = open(os.path.join(self.outpath, self.INFO_FILE), 'a')
        outfile.write('\nTotal voxels included for tiled %u\n' % (total_voxels,))
        if total_voxels > 0:
            self.tiled_priors = [float(x)/total_voxels for x in tiled_count]
            for i in range(self.nlabels):
                outfile.write('label %d %s percentage of voxels = %.8f , count = %d, use prior %.8f\n' \
                    % (i,self.label_names[i],self.tiled_priors[i],tiled_count[i],self.label_priors[i]))
            outfile.write('Sum percentage of allowable tiled voxels = %.3f\n\n' % sum(self.tiled_priors))
        
        # priors again for copy / paste convenience (if using in convnet param file)
        outfile.write('Priors train:   %s\n' % ','.join('%.8f' % i for i in self.label_priors))
        if total_voxels > 0:
            outfile.write('Priors test:    %s\n' % ','.join('%.8f' % i for i in self.tiled_priors))
        if not self.no_label_lookup:
            outfile.write('Priors rand:    %s\n\n' % ','.join('%.8f' % i for i in self.rand_priors))
        
        # other useful info (for debugging / validating outputs)
        outfile.write('data_shape %dx%dx%d ' % self.data_cube.shape)
        outfile.write('labels_shape %dx%dx%d\n' % self.labels_cube.shape)
        outfile.write('num_rand_zslices %d, num_tiled_zslices %d, zslice size %dx%d\n' %\
            (self.size_rand[2], self.nz_tiled, self.size_rand[0], self.size_rand[1]))
        outfile.write('num_cases_per_batch %d, tiles_per_zslice %dx%dx%d\n' %\
            (self.num_cases_per_batch, self.tiles_per_zslice[0], self.tiles_per_zslice[1], 
            self.tiles_per_zslice[2]))
        outfile.write('image_out_size %d, tile_size %dx%dx%d, shape_per_batch %dx%dx%d\n' %\
            (self.image_out_size, self.tile_size[0], self.tile_size[1], self.tile_size[2], 
            self.shape_per_batch[0], self.shape_per_batch[1], self.shape_per_batch[2]))
        outfile.write('data specified mean %.4f, actual mean %.4f, actual std %.4f\n' % (self.EM_mean,
            self.EM_actual_mean, self.EM_actual_std))
        outfile.close()

    def makeTiledIndices(self):
        # xxx - realized that after writing it this way, just evenly dividing the total number of pixels per zslice
        #   would have also probably worked fine. this code is a bit confusing, but it works. basically just makes it
        #   so that each batch is a rectangular tile of a single zslice, instead of some number of pixels in the zslice.
        # this method has also been extended for the case of image_out patches to get multiple z-slices per batch.
        # the method is quite confusing, but it is working so leaving it as is for now, might consider revising this.
    
        if not self.silent: print 'EMDataParser: Creating tiled indices (typically for test and writing outputs)'
        # create the indices for the tiled output - multiple tiles per zslice
        # added the z dimension into the indices so multiple outputs we can have multiple z-slices per batch. 
        #   tile_size is the shape in image output patches for each batch
        #   shape_per_batch is the shape in voxels for each batch
        #   if more than one tile fits in a zslice, the first two dimensions of tiles_per_zslice gives this shape.
        #   if one tile is a single zslice, then the third dim of tiles_per_zslice gives number of zslices in a batch.
        #   swapped the dims so that z is first (so it changes the slowest), need to re-order below when tiling.
        inds_tiled = np.zeros((3,self.tile_size[2],self.tiles_per_zslice[1],self.tiles_per_zslice[0],
            self.tile_size[0],self.tile_size[1]), dtype=self.cubeSubType, order='C')
        for x in range(self.tiles_per_zslice[0]):
            xbeg = self.labels_offset[0] + x*self.shape_per_batch[0] + self.image_out_size/2
            for y in range(self.tiles_per_zslice[1]):
                ybeg = self.labels_offset[1] + y*self.shape_per_batch[1] + self.image_out_size/2
                inds = np.require(np.mgrid[0:self.shape_per_batch[2], xbeg:(xbeg+self.shape_per_batch[0]):\
                    self.image_out_size, ybeg:(ybeg+self.shape_per_batch[1]):self.image_out_size], requirements='C')
                assert( np.logical_and(inds >= 0, inds < self.cubeSubLim).all() )
                inds_tiled[:,:,y,x,:,:] = inds # yes, dims are swapped, this is correct (meh)
        # unswap the dims, xxx - again, maybe this should be re-written in a simpler fashion?
        self.inds_tiled = inds_tiled.reshape((3,self.num_inds_tiled))[[1,2,0],:]
        
        # create another copy that is used for generating the output probabilities (used to be unpackager).
        # this is also confusing using this method of tiling, see comments above.
        # xxx - the transpose is a throwback to how it was written previously in unpackager.
        #   could change subscript order here and in makeOutputCubes, did not see a strong need for this, view is fine
        self.inds_tiled_out = self.inds_tiled.copy().T
        self.inds_tiled_out[:,0] -= self.labels_offset[0]; self.inds_tiled_out[:,1] -= self.labels_offset[1]; 
        self.inds_tiled_out[:,0:2] /= self.image_out_size
        # xxx - these are from the old unpackager, should be self-consistent now, so removed this assert
        #assert( ((self.inds_tiled_out[:,0] >= 0) & (self.inds_tiled_out[:,0] < self.labels_slice_size[0]) & \
        #    (self.inds_tiled_out[:,1] >= 0) & (self.inds_tiled_out[:,1] < self.labels_slice_size[1]) & \
        #    (self.inds_tiled_out[:,2] >= 0)).all() )
        #assert( self.batches_per_zslice*self.num_cases_per_batch == self.inds_tiled_out.shape[0] )

        if self.write_outputs:
            outfile = h5py.File(os.path.join(self.outpath, self.OUTPUT_H5_CVIN), 'a'); 
            outfile.create_dataset('tiled_indices',(3,self.tiles_per_zslice[0]*self.tile_size[0],
                self.tiles_per_zslice[1]*self.tile_size[1]*self.tile_size[2]),data=inds_tiled,compression='gzip', 
                compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
            outfile.close();

    def writeH5Cubes(self):
        print 'EMDataParser: Exporting raw data / labels to hdf5 for validation at "%s"' % (self.outpath,)
        
        outfile = h5py.File(os.path.join(self.outpath, self.OUTPUT_H5_CVIN), 'a'); 
        outfile.create_dataset('data',data=self.data_cube.transpose((2,1,0)),
            compression='gzip', compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
        # copy the attributes over
        for name,value in self.data_attrs.items():
            outfile['data'].attrs.create(name,value)
        if self.labels_cube.size > 0:
            outfile.create_dataset('labels',data=self.labels_cube.transpose((2,1,0)),
                compression='gzip', compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
            outfile.create_dataset('segmented_labels',data=self.segmented_labels_cube.transpose((2,1,0)),
                compression='gzip', compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
            for name,value in self.labels_attrs.items():
                outfile['segmented_labels'].attrs.create(name,value)
        outfile.close();

    # this is the main interface method for fetching batches from the memory cached chunk from the hdf5 file.
    # in normal mode this fetches batches that have been loaded to memory already (see initBatches).
    # in chunklist mode, this can initiate loading a new chunk from a separate location in the hdf5 file.
    # the total processing time here needs to be kept under the network batch time, as the load happens in parallel.
    #def getBatch(self, batchnum, plot_outputs=False, tiledAug=0, do_preprocess=True):
    def getBatch(self, batchnum, plot_outputs=False, tiledAug=0):
        t = time.time()
        
        # allocate batch
        data = np.zeros((self.pixels_per_image, self.num_cases_per_batch), dtype=np.single, order='C')
        aug_data = [None] * self.naug_data
        for i in range(self.naug_data):
            aug_data[i] = np.zeros((self.pixels_per_image, self.num_cases_per_batch), dtype=np.single, order='C')
        if self.no_labels or self.zero_labels:
            labels = np.zeros((0, self.num_cases_per_batch), dtype=np.single, order='C')
            seglabels = np.zeros((0, self.num_cases_per_batch), dtype=self.cubeLblType, order='C')
        else:
            labels = np.zeros((self.noutputs, self.num_cases_per_batch), dtype=np.single, order='C')
            seglabels = np.zeros((self.pixels_per_seg_out, self.num_cases_per_batch), dtype=self.cubeLblType, order='C')

        # get data, augmented data and labels depending on batch type
        if batchnum >= self.FIRST_TILED_BATCH:
            self.getTiledBatch(data,aug_data,labels,seglabels,batchnum,tiledAug)
        elif batchnum >= self.FIRST_RAND_NOLOOKUP_BATCH:
            self.generateRandNoLookupBatch(data,aug_data,labels,seglabels)
        else:
            self.generateRandBatch(data,aug_data,labels,seglabels)
        
        # option to return zero labels, need this when convnet is expecting labels but not using them.
        # this is useful for dumping features over a large area that does not contain labels.
        if self.zero_labels:
            labels = np.zeros((self.noutputs, self.num_cases_per_batch), dtype=np.single, order='C')
        
        '''
        if do_preprocess: 
            if plot_outputs: dataProc = self.preprocessData(data, reScale=False)
            else: data = self.preprocessData(data)
        '''
        
        # replaced preprocessing with scalar mean subtraction and scalar std division.
        if not plot_outputs:
            data -= self.EM_mean if self.EM_mean >= 0 else self.EM_actual_mean
            data /= self.EM_std if self.EM_std > 0 else self.EM_actual_std
            for i in range(self.naug_data):
                aug_data[i] -= self.aug_mean[i] if self.aug_mean[i] >= 0 else self.aug_actual_mean[i]
                aug_data[i] /= self.aug_std[i] if self.aug_std[i] > 0 else self.aug_actual_std[i]
        
        if self.verbose and not self.silent: 
            print 'EMDataParser: Got batch ', batchnum, ' (%.3f s)' % (time.time()-t,)

        # xxx - add another parameter here for different plots?
        if plot_outputs: self.plotDataLbls(data,labels,seglabels,batchnum < self.FIRST_TILED_BATCH)
        #if plot_outputs: self.plotData(data,dataProc,batchnum < self.FIRST_TILED_BATCH)

        #time.sleep(5) # useful for "brute force" memory leak debug
        #return data, labels
        return [data, labels] + aug_data
        
    def generateRandBatch(self,data,aug_data,labels,seglabels):
        assert( not self.no_label_lookup )      # do not use rand batches without label lookup
        if self.use_chunk_list: self.randChunkList()    # load a new cube in chunklist or chunkrange modes
        
        # generate random indices based on lookup table for labels
        lbls = nr.choice(self.nlabels, self.num_cases_per_batch, p=self.label_priors)
        augs = np.bitwise_and(nr.choice(self.NAUGS, self.num_cases_per_batch), self.augs_mask)

        # generate any possible random choices for each label type, will not use all of these, for efficiency
        inds_lbls = np.zeros((self.nlabels, self.num_cases_per_batch), dtype=np.uint64)
        for i in range(self.nlabels): 
            if self.label_lookup_lens[i]==0: continue   # do not attempt to select labels if there are none
            inds_lbls[i,:] = nr.choice(self.label_lookup_lens[i], self.num_cases_per_batch)

        # generate a random offset from the selected location.
        # this prevents the center pixel from being the pixel that is used to select the image every time and
        #   reduces correlations in label selection priors between the center and surrounding
        #   image out patch labels. total possible range of offset comes from image_out_offset parameter.
        # an offset (rand range) larger than image_out_size can help reduce corrleations further.
        # an offset parameter of 1 causes offset to always be zero and so selection is only based on center pixel.
        offset = np.zeros((self.num_cases_per_batch,3), dtype=self.cubeSubType)
        offset[:,0:2] = np.concatenate([x.reshape(self.num_cases_per_batch,1) for x in np.unravel_index(\
            nr.choice(self.pixels_per_out_offset, (self.num_cases_per_batch,)), 
            (self.image_out_offset, self.image_out_offset))], axis=1) - self.image_out_offset/2

        for imgi in range(self.num_cases_per_batch):
            inds = self.inds_label_lookup[lbls[imgi]][inds_lbls[lbls[imgi], imgi],:] + offset[imgi,:]
            #self.getImgDataAtPoint(inds,data[:,imgi],augs[imgi])
            self.getAllDataAtPoint(inds,data,aug_data,imgi,augs[imgi])
            self.getLblDataAtPoint(inds,labels[:,imgi],seglabels[:,imgi],augs[imgi])
        self.tallyTrainingPrior(labels)

    def generateRandNoLookupBatch(self,data,aug_data,labels,seglabels):
        if self.use_chunk_list: self.randChunkList()    # load a new cube in chunklist or chunkrange modes
        
        # generate random indices from anywhere in the rand cube
        if self.no_labels:
            size = self.size_rand; offset = self.labels_offset
        else:
            size = self.size_rand - 2*self.read_border; offset = self.labels_offset + self.read_border
        nrand_inds = 2*self.num_cases_per_batch
        inds = np.concatenate([x.reshape((nrand_inds,1)) for x in np.unravel_index(nr.choice(size.prod(), 
            nrand_inds), size)], axis=1) + offset
            
        # don't select from end slots for multiple zslices per case
        inds = inds[np.logical_and(inds[:,2] >= self.nzslices/2, inds[:,2] < self.nrand_zslice-self.nzslices/2),:]
        augs = np.bitwise_and(nr.choice(self.NAUGS, self.num_cases_per_batch), self.augs_mask)
        
        for imgi in range(self.num_cases_per_batch):
            #self.getImgDataAtPoint(inds[imgi,:],data[:,imgi],augs[imgi])
            self.getAllDataAtPoint(inds[imgi,:],data,aug_data,imgi,augs[imgi])
            if not self.no_labels:
                self.getLblDataAtPoint(inds[imgi,:],labels[:,imgi],seglabels[:,imgi],augs[imgi])
        if not self.no_labels: self.tallyTrainingPrior(labels)

    def tallyTrainingPrior(self, labels):
        if 'prior_train_count' not in self.batch_meta: return
        
        # training label counts for calculating prior are allocated in convnet layers.py harness
        #   so that they can be stored in the convnet checkpoints.
        self.batch_meta['prior_total_count'] += self.num_cases_per_batch
        if self.independent_labels:
            self.batch_meta['prior_train_count'] += labels.astype(np.bool).sum(axis=1)
        else:
            cnts,edges = np.histogram(labels.astype(np.int32), bins=range(0,self.nlabels+1), range=(0,self.nlabels))
            self.batch_meta['prior_train_count'] += cnts

    def getTiledBatchOffset(self, batchnum, setChunkList=False):
        assert( batchnum >= self.FIRST_TILED_BATCH )    # this is only for tiled batches
        
        # these conversions used to be in data.cu for GPU data provider
        batchOffset = batchnum - self.FIRST_TILED_BATCH

        # for chunklist mode, the batch also determines which chunk we are in. need to reload if moving to new chunk
        if self.use_chunk_list:
            chunk = batchOffset / self.batches_per_rand_cube; batchOffset %= self.batches_per_rand_cube

            # xxx - moved this here so don't need this requirement for rand, doesn't matter b/c randomly selected.
            #   it is possible to fix this for tiled, but doesn't seem necessary.
            #   if it's fixed need to decide what are the chunks... still easier to have them as actual Knossos-sized
            #     chunks and not as defined by size_rand, then have to change how chunk_range_index is incremented
            assert( not self.use_chunk_range or (self.size_rand == self.chunksize).all() )

            # draw from the list of tiled chunks only, set in init depending on parameters.
            if setChunkList: self.setChunkList(chunk, self.chunk_tiled_list)
        else:
            chunk = None
            
        return batchOffset, chunk
        
    def getTiledBatch(self, data,aug_data,labels,seglabels, batchnum, aug=0):
        batchOffset, chunk = self.getTiledBatchOffset(batchnum, setChunkList=True)
        
        # get index and zslice. same method for regular or use_chunk_list modes.   
        ind0 = (batchOffset % self.batches_per_zslice)*self.num_cases_per_batch
        zslc = batchOffset / self.batches_per_zslice * self.zslices_per_batch + self.nzslices/2;
        assert( zslc < self.ntotal_zslice ) # usually fails then specified tiled batch is out of range of cube

        inds = np.zeros((3,),dtype=self.cubeSubType)
        for imgi in range(self.num_cases_per_batch):
            inds[:] = self.inds_tiled[:,ind0 + imgi]; inds[2] += zslc
            #self.getImgDataAtPoint(inds,data[:,imgi],aug)
            self.getAllDataAtPoint(inds,data,aug_data,imgi,aug)
            self.getLblDataAtPoint(inds,labels[:,imgi],seglabels[:,imgi],aug)

    # set to a specific chunk, re-initBatches if the new chunk is different from the current one
    def setChunkList(self, chunk, chunk_list):
        # should only be called from chunklist or chunkrange modes
        assert(chunk >= 0 and chunk < len(chunk_list))   # usually fails when tiled batch is out of range of chunks
        chunk = chunk_list[chunk]   # chunk looks up in appropriate list (for rand or tiled chunks), set by ini
        if self.use_chunk_range:
            self.chunk_list_index = np.nonzero(chunk >= self.chunk_range_cumsize)[0][-1]
            self.chunk_range_index = chunk - self.chunk_range_cumsize[self.chunk_list_index]
            chunk_rand = np.unravel_index(self.chunk_range_index, self.chunk_range_rng[self.chunk_list_index,:]) \
                + self.chunk_range_beg[self.chunk_list_index,:]
            offset_rand = self.offset_list[self.chunk_list_index,:]
        else:
            self.chunk_list_index = chunk
            chunk_rand = self.chunk_range_beg[chunk,:]; offset_rand = self.offset_list[chunk,:]
            
        # compare with actual chunks and offsets here instead of index to avoid loading the first chunk twice
        if (chunk_rand != self.chunk_rand).any() or (offset_rand != self.offset_rand).any():
            if self.last_chunk_rand is None:
                # special case incase there is no next chunk ever loaded (only one chunk being written)
                self.last_chunk_rand = chunk_rand; self.last_offset_rand = offset_rand; 
            else:
                self.last_chunk_rand = self.chunk_rand; self.last_offset_rand = self.offset_rand; 
            self.chunk_rand = chunk_rand; self.offset_rand = offset_rand; 
            self.initBatches(silent=not self.verbose)
        elif self.last_chunk_rand is None: 
            # special case incase there is no next chunk ever loaded (only one chunk being written)
            self.last_chunk_rand = chunk_rand; self.last_offset_rand = offset_rand; 
        
    def randChunkList(self):
        assert( self.chunk_range_rand > 0 )     # do not request rand chunk with zero range
        # should only be called from chunklist or chunkrange modes
        if self.chunk_list_rand:
            nextchunk = random.randrange(self.chunk_range_rand)
        else:
            if self.use_chunk_range: nextchunk = (self.chunk_range_index+1) % self.chunk_range_rand
            else: nextchunk = (self.chunk_list_index+1) % self.chunk_range_rand
        # draw from the list of random chunks only, set in init depending on parameters.
        self.setChunkList(nextchunk, self.chunk_rand_list)

    def getAllDataAtPoint(self,inds,data,aug_data,imgi,aug=0):
        self.getImgDataAtPoint(self.data_cube,inds,data[:,imgi],aug)
        for i in range(self.naug_data):
            self.getImgDataAtPoint(self.aug_data[i],inds,aug_data[i][:,imgi],aug)
        
    def getImgDataAtPoint(self,data_cube,inds,data,aug):
        # don't simplify this... it's integer math
        selx = slice(inds[0]-self.image_size/2,inds[0]-self.image_size/2+self.image_size)
        sely = slice(inds[1]-self.image_size/2,inds[1]-self.image_size/2+self.image_size)
        selz = slice(inds[2]-self.nzslices/2,inds[2]-self.nzslices/2+self.nzslices)
        #print data_cube[selx,sely,selz].shape, inds
        data[:] = EMDataParser.augmentData(data_cube[selx,sely,selz].astype(np.single),
            aug).transpose(2,0,1).flatten('C')  # z last because channel data must be contiguous for convnet

    def getLblDataAtPoint(self,inds,labels,seglabels,aug=0):
        if labels.size == 0: return
        # some conditions can not happen here, and this should have been asserted in getLabelMap
        if not self.independent_labels:
            assert( self.noutputs == 1 ) # just make sure
            # image out size has to be one in this case, just take the center label
            # the image in and out size must both be even or odd for this to work (asserted in init)
            indsl = inds - self.labels_offset; labels[:] = self.labels_cube[indsl[0],indsl[1],indsl[2]]
            seglabels[:] = self.segmented_labels_cube[indsl[0],indsl[1],indsl[2]]
        else:
            # NOTE from getLabelMap for border: make 3d for (convenience) in ortho reslice code, but always need same 
            #   in xy dir and zero in z for lbl slicing. xxx - maybe too confusing, fix to just use scalar? 
            b = self.segmented_labels_border; indsl = inds - self.labels_offset + b
            # don't simplify this... it's integer math
            selx = slice(indsl[0]-self.seg_out_size/2,indsl[0]-self.seg_out_size/2+self.seg_out_size)
            sely = slice(indsl[1]-self.seg_out_size/2,indsl[1]-self.seg_out_size/2+self.seg_out_size)
            lbls = EMDataParser.augmentData(self.segmented_labels_cube[selx,sely,indsl[2]].reshape((self.seg_out_size,
                self.seg_out_size,1)),aug).reshape((self.seg_out_size,self.seg_out_size))
            seglabels[:] = lbls.flatten('C')
            # xxx - currently affinity labels assume a single pixel border around the segmented labels only.
            #   put other views here if decide to expand label border for selection (currently does not seem neccessary)
            # also see note above, might be better to make segmented_labels_border a scalar
            lblscntr = lbls[b[0]:-b[0],b[1]:-b[1]] if b[0] > 0 else lbls

            # change the view on the output for easy assignment
            lblsout = labels.reshape((self.image_out_size,self.image_out_size,self.nIndepLabels))
            # this code needs to be consistent with (independent) label meanings defined in getLabelMap
            if self.label_type == 'ICSorOUT':
                if self.image_out_size==1:
                    # need at least two outputs for convnet (even if independent)
                    lblsout[lblscntr >  0,0] = 1;   # ICS
                    lblsout[lblscntr == 0,1] = 1;   # OUT
                else:
                    lblsout[lblscntr >  0,0] = 1;   # ICS
            elif self.label_type == 'ICSorECSorMEM':
                lblsout[np.logical_and(lblscntr > 0,lblscntr != self.ECS_label_value),0] = 1;   # ICS
                lblsout[lblscntr == self.ECS_label_value,1] = 1;                                # ECS
                lblsout[lblscntr == 0,2] = 1;                                                   # MEM
            elif self.label_type == 'ICSorECS':
                lblsout[np.logical_and(lblscntr > 0,lblscntr != self.ECS_label_value),0] = 1;   # ICS
                lblsout[lblscntr == self.ECS_label_value,1] = 1;                                # ECS
            elif self.label_type == 'ICSorMEM':
                lblsout[np.logical_and(lblscntr > 0,lblscntr != self.ECS_label_value),0] = 1;   # ICS
                lblsout[lblscntr == 0,1] = 1;                                                   # MEM
            elif self.label_type == 'affin2':
                isICS = (lblscntr > 0)
                lblsout[np.logical_and(isICS,np.diff(lbls[1:,1:-1],1,0) == 0),0] = 1; 
                lblsout[np.logical_and(isICS,np.diff(lbls[1:-1,1:],1,1) == 0),1] = 1;
            elif self.label_type == 'affin4':
                diff0 = np.diff(lbls[1:,1:-1],1,0)==0; diff1 = np.diff(lbls[1:-1,1:],1,1)==0
                # affinities for ICS voxels
                isICS = np.logical_and(lblscntr > 0, lblscntr != self.ECS_label_value)
                lblsout[np.logical_and(isICS,diff0),0] = 1; lblsout[np.logical_and(isICS,diff1),1] = 1;
                # affinities for ECS voxels
                isECS = (lblscntr == self.ECS_label_value)
                lblsout[np.logical_and(isECS,diff0),2] = 1; lblsout[np.logical_and(isECS,diff1),3] = 1;
            elif self.label_type == 'affin6':
                diff0 = np.diff(lbls[1:,1:-1],1,0)==0; diff1 = np.diff(lbls[1:-1,1:],1,1)==0
                # affinities for ICS voxels
                isICS = np.logical_and(lblscntr > 0, lblscntr != self.ECS_label_value)
                lblsout[np.logical_and(isICS,diff0),0] = 1; lblsout[np.logical_and(isICS,diff1),1] = 1;
                # affinities for ECS voxels
                isECS = (lblscntr == self.ECS_label_value)
                lblsout[np.logical_and(isECS,diff0),2] = 1; lblsout[np.logical_and(isECS,diff1),3] = 1;
                # affinities for MEM voxels
                isMEM = (lblscntr == 0)
                lblsout[np.logical_and(isMEM,diff0),4] = 1; lblsout[np.logical_and(isMEM,diff1),5] = 1;
    
    # originally this was single function for loading em data and labels.
    # split into reading of labels and reading of data so that extra data can be read, i.e., augmented data        
    #
    # Comments from original function regarding how data is loading to support reslices and C/F order:
    # xxx - might think of a better way to "reslice" the dimensions later, for now, here's the method:
    # read_direct requires the same size for the numpy array as in the hdf5 file. so if we're re-ordering the dims:
    #   (1) re-order the sizes to allocate here as if in original xyz order. 
    #   (2) re-order the dims and sizes used in the *slices_from_indices functions into original xyz order. 
    #       chunk indices are not changed.
    #   (3) at the end of this function re-order the data and labels into the specified dim ordering
    #   (4) the rest of the packager is then blind to the reslice dimension ordering
    # NOTE ORIGINAL: chunk indices should be given in original hdf5 ordering.
    #   all other command line arguments should be given in the re-ordered ordering.
    #   the C/F order re-ordering needs to be done nested inside the reslice re-ordering
    # NEW NOTE: had the re-ordering of command line inputs for reslice done automatically, meaning all inputs on 
    #   command line should be given in original ordering, but they are re-ordered in re-slice order in init, so
    #   un-re-order here to go back to original ordering again (minimal overhead, done to reduce debug time).
    #
    # ulimately everything is accessed as C-order, but support loading from F-order hdf5 inputs.
    # h5py requires that for read_direct data must be C order and contiguous. this means F-order must be dealt with 
    #   "manually". for F-order the cube will be in C-order, but shaped like F-order, and then the view 
    #   transposed back to C-order so that it's transparent in the rest of the code.
    def readCubeToBuffers(self):
        if not self.silent: print 'EMDataParser: Buffering data and labels chunk %d,%d,%d offset %d,%d,%d' % \
            (self.chunk_rand[0], self.chunk_rand[1], self.chunk_rand[2], 
            self.offset_rand[0], self.offset_rand[1], self.offset_rand[2])

        self.data_cube, self.data_attrs, self.chunksize, self.datasize, self.EM_actual_mean, self.EM_actual_std = \
            self.loadData( self.data_cube if hasattr(self, 'data_cube') else None, self.imagesrc, self.dataset )
        self.loadSegmentedLabels()

        # load augmented data cubes
        for i in range(self.naug_data):
            self.aug_data[i], data_attrs, chunksize, datasize, self.aug_actual_mean[i], self.aug_actual_std[i] = \
                self.loadData( self.aug_data[i], self.augsrc, self.aug_datasets[i] )
            if not self.silent: print '\tbuffered aug data ' + self.aug_datasets[i]

    def loadData(self, data_cube, fname, dataset):
        data_size = list(self.data_slice_size[i] for i in self.zreslice_dim_ordering)
        size_rand = self.size_rand[self.zreslice_dim_ordering]; size_tiled = self.size_tiled[self.zreslice_dim_ordering]
        if self.verbose and not self.silent: print 'data slice size ' + str(self.data_slice_size) + \
            ' data size ' + str(data_size) + ' size rand ' + str(size_rand) + ' size tiled ' + str(size_tiled)

        hdf = h5py.File(fname,'r')
        if data_cube is None:
            # for chunkrange / chunklist mode, this function is recalled, don't reallocate in this case
            if self.hdf5_Corder: 
                data_cube = np.zeros(data_size, dtype=hdf[dataset].dtype, order='C')
            else: 
                data_cube = np.zeros(data_size[::-1], dtype=hdf[dataset].dtype, order='C')
        else:
            # change back to the original view (same view changes as below, opposite order)

            # zreslice un-re-ordering, so data is in original view in this function           
            data_cube = data_cube.transpose(self.zreslice_dim_ordering)

            # the C/F order re-ordering needs to be done nested inside the reslice re-ordering
            if not self.hdf5_Corder: 
                data_cube = data_cube.transpose(2,1,0)

        # slice out the data hdf
        ind = self.get_hdf_index_from_chunk_index(hdf[dataset], self.chunk_rand, self.offset_rand)
        slc,slcd = self.get_data_slices_from_indices(ind, size_rand, data_size, False)
        hdf[dataset].read_direct(data_cube, slc, slcd)
        if self.nz_tiled > 0:
            ind = self.get_hdf_index_from_chunk_index(hdf[dataset], self.chunk_tiled, self.offset_tiled)
            slc,slcd = self.get_data_slices_from_indices(ind, size_tiled, data_size, True)
            hdf[dataset].read_direct(data_cube, slc, slcd)
        data_attrs = {}
        for name,value in hdf[dataset].attrs.items(): data_attrs[name] = value
        # xxx - this is only used for chunkrange mode currently, likely item to rethink...
        chunksize = np.array(hdf[dataset].chunks, dtype=np.int64)
        datasize = np.array(hdf[dataset].shape, dtype=np.int64)  # not currently used
        hdf.close()

        # calculate mean and std over all of the data cube
        mean = float(data_cube.mean(dtype=np.float64)); std = float(data_cube.std(dtype=np.float64))

        # the C/F order re-ordering needs to be done nested inside the reslice re-ordering
        if not self.hdf5_Corder: 
            data_cube = data_cube.transpose(2,1,0)

        # zreslice re-ordering, so data is in re-sliced order view outside of this function           
        data_cube = data_cube.transpose(self.zreslice_dim_ordering)
        if self.verbose and not self.silent: 
            print 'After re-ordering ' + fname + ' ' + dataset + ' data cube shape ' + str(data_cube.shape)
            
        return data_cube, data_attrs, chunksize, datasize, mean, std

    def loadSegmentedLabels(self):
        if self.no_labels: seglabels_size = [0, 0, 0]
        else: seglabels_size = list(self.segmented_labels_slice_size[i] for i in self.zreslice_dim_ordering)
        size_rand = self.size_rand[self.zreslice_dim_ordering]; size_tiled = self.size_tiled[self.zreslice_dim_ordering]
        if self.verbose and not self.silent: print 'seglabels size ' + str(seglabels_size) + \
            ' size rand ' + str(size_rand) + ' size tiled ' + str(size_tiled)
            
        if not hasattr(self, 'segmented_labels_cube'):
            # for chunkrange / chunklist mode, this function is recalled, don't reallocate in this case
            if self.hdf5_Corder: 
                self.segmented_labels_cube = np.zeros(seglabels_size, dtype=self.cubeLblType, order='C')
            else: 
                self.segmented_labels_cube = np.zeros(seglabels_size[::-1], dtype=self.cubeLblType, order='C')
        else:
            # change back to the original view (same view changes as below, opposite order)

            # zreslice un-re-ordering, so data is in original view in this function           
            self.segmented_labels_cube = self.segmented_labels_cube.transpose(self.zreslice_dim_ordering)

            # the C/F order re-ordering needs to be done nested inside the reslice re-ordering
            if not self.hdf5_Corder: 
                self.segmented_labels_cube = self.segmented_labels_cube.transpose(2,1,0)

        # slice out the labels hdf except for no_labels mode (save memory)
        hdf = h5py.File(self.labelsrc,'r');
        if not self.no_labels: 
            ind = self.get_hdf_index_from_chunk_index(hdf[self.username], self.chunk_rand, self.offset_rand)
            slc,slcd = self.get_label_slices_from_indices(ind, size_rand, seglabels_size, False)
            hdf[self.username].read_direct(self.segmented_labels_cube, slc, slcd)
            if self.nz_tiled > 0:
                ind = self.get_hdf_index_from_chunk_index(hdf[self.username], self.chunk_tiled, self.offset_tiled)
                slc,slcd = self.get_label_slices_from_indices(ind, size_tiled, seglabels_size, True)
                hdf[self.username].read_direct(self.segmented_labels_cube, slc, slcd)
        self.labels_attrs = {}
        for name,value in hdf[self.username].attrs.items(): self.labels_attrs[name] = value
        assert( (self.chunksize == np.array(hdf[self.username].chunks, dtype=np.int64)).all() )
        hdf.close()
        
        # the C/F order re-ordering needs to be done nested inside the reslice re-ordering
        if not self.hdf5_Corder: 
            self.segmented_labels_cube = self.segmented_labels_cube.transpose(2,1,0)

        # zreslice re-ordering, so data is in re-sliced order view outside of this function           
        self.segmented_labels_cube = self.segmented_labels_cube.transpose(self.zreslice_dim_ordering)
        if self.verbose and not self.silent: print 'After re-ordering segmented labels cube shape ' + \
            str(self.segmented_labels_cube.shape)

    def get_hdf_index_from_chunk_index(self, hdf_dataset, chunk_index, offset):
        datasize = np.array(hdf_dataset.shape, dtype=np.int64)
        chunksize =  np.array(hdf_dataset.chunks, dtype=np.int64)
        nchunks = datasize/chunksize
        if self.hdf5_Corder: ci = chunk_index
        else: ci = chunk_index[::-1]
        # chunk index is either given as origin-centered, or zero-based relative to corner
        if self.origin_chunk_inds: ci = (ci + nchunks/2 + nchunks%2 - 1) # origin-centered chunk index
        # always return the indices into the hdf5 in C-order
        if self.hdf5_Corder: return ci*chunksize + offset
        else: return (ci*chunksize)[::-1] + offset

    # xxx - add asserts to check that data select is inbounds in hdf5, currently not a graceful error
    def get_data_slices_from_indices(self, ind, size, dsize, isTiled):
        xysel = self.zreslice_dim_ordering[0:2]; zsel = self.zreslice_dim_ordering[2]
        beg = ind; end = ind + size
        beg[xysel] = beg[xysel] - self.image_size/2; beg[zsel] = beg[zsel] - self.nzslices/2
        end[xysel] = end[xysel] + self.image_size/2; end[zsel] = end[zsel] + self.nzslices/2
        return self.get_slices_from_limits(beg,end,dsize,isTiled)
        
    # xxx - add asserts to check that labels select is inbounds in hdf5, currently not a graceful error
    def get_label_slices_from_indices(self, ind, size, dsize, isTiled):
        xysel = self.zreslice_dim_ordering[0:2]; zsel = self.zreslice_dim_ordering[2]
        beg = ind - self.segmented_labels_border[self.zreslice_dim_ordering] 
        end = ind + size + self.segmented_labels_border[self.zreslice_dim_ordering]
        beg[zsel] = beg[zsel] - self.nzslices/2; end[zsel] = end[zsel] + self.nzslices/2
        return self.get_slices_from_limits(beg,end,dsize,isTiled)

    def get_slices_from_limits(self, beg, end, size, isTiled):
        zsel = self.zreslice_dim_ordering[2]
        begd = np.zeros_like(size); endd = size;
        if isTiled: 
            begd[zsel], endd[zsel] = self.nrand_zslice, self.ntotal_zslice
        else: 
            begd[zsel], endd[zsel] = 0, self.nrand_zslice
        if self.hdf5_Corder: 
            slc = np.s_[beg[0]:end[0],beg[1]:end[1],beg[2]:end[2]]
            slcd = np.s_[begd[0]:endd[0],begd[1]:endd[1],begd[2]:endd[2]]
        else:
            slc = np.s_[beg[2]:end[2],beg[1]:end[1],beg[0]:end[0]]
            slcd = np.s_[begd[2]:endd[2],begd[1]:endd[1],begd[0]:endd[0]]
        return slc,slcd

    # Don't need to pickle meta anymore as parser is instantiated and runs on-demand from EMDataProvider.
    def makeBatchMeta(self):
        if self.no_labels:
            noutputs = 0; label_names = []
        else:
            noutputs = self.noutputs
            label_names = self.indep_label_names_out if self.independent_labels else self.label_names
        #data_mean = self.EM_mean if self.EM_mean >= 0 else self.EM_actual_mean
        #data_std = self.EM_std if self.EM_std > 0 else self.EM_actual_std
        # do not re-assign meta dict so this works with chunklist mode (which reloads each time)
        if not hasattr(self, 'batch_meta'): self.batch_meta = {}
        b = self.batch_meta; b['num_cases_per_batch']=self.num_cases_per_batch; b['label_names']=label_names; 
        b['nlabels']=len(label_names); b['pixels_per_image']=self.pixels_per_image; 
        #b['scalar_data_mean']=data_mean; b['scalar_data_std']=data_std; 
        b['noutputs']=noutputs; b['num_pixels_per_case']=self.pixels_per_image;
        if self.verbose and not self.silent: print self.batch_meta
        
        # for debug only (standalone harness), DO NOT UNcomment these when running network, then count won't be saved
        #self.batch_meta['prior_train_count'] = np.zeros((self.noutputs if self.independent_labels else self.nlabels,),
        #    dtype=np.int64)
        #self.batch_meta['prior_total_count'] = np.zeros((1,),dtype=np.int64)

    # labels_cube is used for label priors for selecting pixels for presentation to convnet.
    # The actual labels are calculated on-demand using the segmented labels (not using the labels created here),
    #   unless NOT independent_labels in which case the labels cube is also the labels sent to the network.
    def setupLabels(self):
        # init labels to empty and return if no label mode
        if self.no_labels: 
            self.labels_cube = np.zeros((0,0,0), dtype=self.cubeLblType, order='C'); return
            
        num_empty = (self.segmented_labels_cube == self.EMPTY_LABEL).sum()
        assert( self.no_label_lookup or num_empty != self.segmented_labels_cube.size ) # a completely unlabeled chunk
        if num_empty > 0:
            if not self.silent: print 'EMDataParser: WARNING: %d empty label voxels selected' % float(num_empty)

        # ECS as a single label is used for some of the label types. figure out which label it is based on ini param
        # need a separate variable for chunklist mode where the ECS label in different regions is likely different.
        # xxx - this does not work in the cases where the ECS label has been set differently in adjacent regions.
        #   would likely have to go back to a fixed label for this, but not clear why this situation would come up.
        if self.ECS_label == -2:
            self.ECS_label_value = self.ECS_LABEL     # specifies ECS label is single defined value (like EMPTY_LABEL)
        elif self.ECS_label == -1:
            # specifies that ECS is labeled with whatever the last label is, ignore the empty label
            self.ECS_label_value = (self.segmented_labels_cube[self.segmented_labels_cube != self.EMPTY_LABEL]).max()
        else:
            self.ECS_label_value = self.ECS_label     # use supplied value for ECS

        if not hasattr(self, 'labels_cube'):
            # do not re-allocate if just setting up labels for a new cube for cubelist / cuberange mode
            self.labels_cube = np.zeros(self.labels_slice_size, dtype=self.cubeLblType, order='C')

        if self.select_label_type == 'ICS_OUT':
            self.labels_cube[self.segmented_labels_cube == 0] = self.labels['OUT']
            self.labels_cube[self.segmented_labels_cube >  0] = self.labels['ICS']
        elif self.select_label_type == 'ICS_ECS_MEM':
            self.labels_cube[self.segmented_labels_cube == 0] = self.labels['MEM']
            self.labels_cube[np.logical_and(self.segmented_labels_cube > 0, 
                self.segmented_labels_cube != self.ECS_label_value)] = self.labels['ICS']
            self.labels_cube[self.segmented_labels_cube == self.ECS_label_value] = self.labels['ECS']
        elif self.select_label_type == 'ICS_OUT_BRD':
            self.labels_cube[:] = self.labels['ICS']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:,1:-1],1,0) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[0:-1,1:-1],1,0) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:-1,1:],1,1) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:-1,0:-1],1,1) != 0] = self.labels['BORDER']
            # xxx - this would highlight membrane areas that are near border also, better method of balancing priors?
            self.labels_cube[np.logical_and(self.segmented_labels_cube[1:-1,1:-1] == 0,
                self.labels_cube != self.labels['BORDER'])] = self.labels['OUT']
            #self.labels_cube[self.segmented_labels_cube[1:-1,1:-1] == 0] = self.labels['OUT']
        elif self.select_label_type == 'ICS_ECS_MEM_BRD':
            self.labels_cube[:] = self.labels['ICS']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:,1:-1],1,0) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[0:-1,1:-1],1,0) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:-1,1:],1,1) != 0] = self.labels['BORDER']
            self.labels_cube[np.diff(self.segmented_labels_cube[1:-1,0:-1],1,1) != 0] = self.labels['BORDER']
            # xxx - this would highlight membrane areas that are near border also, better method of balancing priors?
            self.labels_cube[np.logical_and(self.segmented_labels_cube[1:-1,1:-1] == 0,
                self.labels_cube != self.labels['BORDER'])] = self.labels['MEM']
            #self.labels_cube[self.segmented_labels_cube[1:-1,1:-1] == 0] = self.labels['MEM']
            self.labels_cube[self.segmented_labels_cube[1:-1,1:-1] == self.ECS_label_value] = self.labels['ECS']
        else:
            raise Exception('Unknown select_label_type ' + self.select_label_type)

    def getLabelMap(self):
        # NEW: change how labels work so that labels that use priors for selection are seperate from how the labels are
        #   sent / interpreted by the network. First setup select_label_type maps.
        # NOTE: the select labels are also the labels sent to the network if NOT independent_labels.
        #   in this case there also must only be one output voxel (multiple outputs not supported for
        #     mutually exclusive labels.
        
        border = 0  # this is for label selects or label types that need bordering pixels around cube to fetch labels
        # label names need to be in the same order as the indices in the labels map.
        if self.select_label_type == 'ICS_OUT':
            self.labels = {'OUT':0, 'ICS':1} # labels are binary, intracellular or outside (not intracellular)
            self.label_names = ['OUT', 'ICS']
        elif self.select_label_type == 'ICS_ECS_MEM':
            self.labels = {'MEM':0, 'ICS':1, 'ECS':2} # label values for ICS, MEM and ECS fixed values
            self.label_names = ['MEM', 'ICS', 'ECS']
        elif self.select_label_type == 'ICS_OUT_BRD':
            # for the priors for affinity just use three classes, OUT, ICS or border voxels
            self.labels = {'OUT':0, 'BORDER':1, 'ICS':2}
            self.label_names = ['OUT', 'BORDER', 'ICS']
            border = 1
        elif self.select_label_type == 'ICS_ECS_MEM_BRD':
            # for the priors for affinity just use three classes, OUT, ICS or border voxels
            self.labels = {'MEM':0, 'BORDER':1, 'ICS':2, 'ECS':3}
            self.label_names = ['MEM', 'BORDER', 'ICS', 'ECS']
            border = 1

        # then setup "independent label names" which will be used to setup how labels are sent to network.
        # these are the actual labels, the ones above are used for selecting voxels randomly based on label lookup 
        #   using the label prior specified in the ini file (balancing).   
        if self.label_type == 'ICSorOUT':
            if self.image_out_size==1:
                # for single pixel output, use two independent outputs (convnet doesn't like single output)
                self.indep_label_names = ['ICS', 'OUT']
            else:
                # one output per pixel
                self.indep_label_names = ['ICS']
        elif self.label_type == 'ICSorECSorMEM':
            self.indep_label_names = ['ICS', 'ECS', 'MEM']
        elif self.label_type == 'ICSorECS':
            assert( self.independent_labels )   # this setup is intended for MEM to be encoded by no winner
            # the labels used for selection are still the same, just learn outputs as 0, 0 for membrane
            self.indep_label_names = ['ICS', 'ECS']
        elif self.label_type == 'ICSorMEM':
            assert( self.independent_labels )   # this setup is intended for ECS to be encoded by no winner
            # the labels used for selection are still the same, just learn outputs as 0, 0 for ecs
            self.indep_label_names = ['ICS', 'MEM']
        elif self.label_type == 'affin2':
            assert( self.independent_labels )   # xxx - can construct mutex labels, but decided no point to support this
            # two output per pixel, the affinities in two directions
            self.indep_label_names = ['DIM0POS', 'DIM1POS']
            border = 1
        elif self.label_type == 'affin4':
            assert( self.independent_labels )   # xxx - can construct mutex labels, but decided no point to support this
            # four outputs per pixel, the affinities in two directions for ICS and for ECS
            self.indep_label_names = ['ICS_DIM0POS', 'ICS_DIM1POS', 'ECS_DIM0POS', 'ECS_DIM1POS']
            border = 1
        elif self.label_type == 'affin6':
            assert( self.independent_labels )   # xxx - can construct mutex labels, but decided no point to support this
            # six outputs per pixel, the affinities in two directions for ICS, ECS and MEM
            self.indep_label_names = ['ICS_DIM0POS', 'ICS_DIM1POS', 'ECS_DIM0POS', 'ECS_DIM1POS', 
                'MEM_DIM0POS', 'MEM_DIM1POS']
            border = 1
        else:
            raise Exception('Unknown label_type ' + self.label_type)

        # this is for affinity graphs so there is no boundary problem at edges of segmented labels, default no boundary
        # make 3d for (convenience) in ortho reslice code, but always need same in xy dir and zero in z for lbl slicing
        self.segmented_labels_border = np.zeros((3,),dtype=np.int32); self.segmented_labels_border[0:2] = border
        assert( self.independent_labels or border==0 )  # did not see the point in supporting this

        self.nlabels = len(self.label_names)
        self.nIndepLabels = len(self.indep_label_names)
        self.indep_label_names_out = []
        for i in range(self.pixels_per_out_image): 
            for j in range(self.nIndepLabels):
                self.indep_label_names_out.append('%s_%d' % (self.indep_label_names[j], i))
        self.noutputs = len(self.indep_label_names_out) if self.independent_labels else 1

    # plotting code to validate that data / labels are being created / selected correctly
    # matplotlib imshow does not swap the axes so need transpose to put first dim on x-axis (like in imagej, itk-snap)
    def plotDataLbls(self,data,labels,seglabels,pRand=True,doffset=0.0):
        imgno = -1; interp_string = 'nearest' # 'none' not supported by slightly older version of matplotlib (meh)
        # just keep bring up plots with EM data sample from batch range
        while True:
            pl.figure(1);
            if ((not self.independent_labels or self.image_out_size==1) and self.label_type[0:6] != 'affin') or \
                labels.size == 0:
                # original mode, softmax labels for single output pixel
                for i in range(4):
                    imgno = random.randrange(self.num_cases_per_batch) if pRand else imgno+1
                    pl.subplot(2,2,i+1)
                    # this is ugly, but data was previously validated using this plotting, so kept it, also below
                    slc = data[:,imgno].reshape(self.pixels_per_image,1).\
                        reshape(self.nzslices, self.image_size, self.image_size)[self.nzslices/2,:,:].\
                        reshape(self.image_size, self.image_size, 1) + doffset
                    # Repeat for the three color channels so plotting can occur normally (written for color images).
                    img = np.require(np.concatenate((slc,slc,slc), axis=2) / (255.0 if slc.max() > 1 else 1), 
                        dtype=np.single)
                    if labels.size > 0:
                        # Put a red dot at the center pixel
                        img[self.image_size/2,self.image_size/2,0] = 1; 
                        img[self.image_size/2,self.image_size/2,1] = 0;
                        img[self.image_size/2,self.image_size/2,2] = 0;
                    pl.imshow(img.transpose((1,0,2)),interpolation=interp_string);
                    if labels.size == 0:
                        pl.title('imgno %d' % imgno)
                    elif not self.independent_labels:
                        pl.title('label %s (%d), imgno %d' % (self.label_names[np.asscalar(labels[0,
                            imgno].astype(int))], np.asscalar(seglabels[0,imgno].astype(int)), imgno))
                    else: 
                        lblstr = ' '.join(self.indep_label_names[s] for s in np.nonzero(labels[:,imgno])[0].tolist())
                        pl.title('label %s (%d), imgno %d' % (lblstr,np.asscalar(seglabels[0,imgno].astype(int)),imgno))
            else:
                # new mode, multiple output pixels, make two plots
                for i in range(2):
                    imgno = random.randrange(self.num_cases_per_batch) if pRand else imgno+1
                    slc = data[:,imgno].reshape(self.pixels_per_image,1).\
                        reshape(self.nzslices, self.image_size, self.image_size)[self.nzslices/2,:,:].\
                        reshape(self.image_size, self.image_size, 1) + doffset
                    # Repeat for the three color channels so plotting can occur normally (written for color images).
                    img = np.require(np.concatenate((slc,slc,slc), axis=2) / 255.0, dtype=np.single)
                    imgA = np.require(np.concatenate((slc,slc,slc,
                        np.ones((self.image_size,self.image_size,1))*255), axis=2) / 255.0, dtype=np.single)
                    
                    alpha = 0.5 # overlay (independent) labels with data
                    pl.subplot(2,2,2*i+1)
                    lbls = labels[:,imgno].reshape((self.image_out_size,self.image_out_size,self.nIndepLabels))
                    print lbls[:,:,0].reshape((self.image_out_size,self.image_out_size))
                    if self.nIndepLabels > 1:
                        print lbls[:,:,1].reshape((self.image_out_size,self.image_out_size))
                    assert(self.nIndepLabels < 4) # need more colors for this
                    osz = self.image_out_size
                    rch = lbls[:,:,0].reshape(osz,osz,1) 
                    gch = lbls[:,:,1].reshape(osz,osz,1) if self.nIndepLabels > 1 else np.zeros((osz,osz,1))
                    bch = lbls[:,:,2].reshape(osz,osz,1) if self.nIndepLabels > 2 else np.zeros((osz,osz,1))
                    if alpha < 1:
                        pl.imshow(imgA.transpose((1,0,2)),interpolation=interp_string); pl.hold(True)
                        imglbls = np.concatenate((rch,gch,bch,np.ones((osz,osz,1))*alpha), axis=2).astype(np.single);
                        imglbls[(lbls==0).all(2),3] = 0    # make background clear
                        img3 = np.zeros((self.image_size, self.image_size, 4), dtype=np.single, order='C')
                        b = self.image_size/2-osz/2; slc = slice(b,b+osz); img3[slc,slc,:] = imglbls;
                        pl.imshow(img3.transpose((1,0,2)),interpolation=interp_string)
                    else:
                        imglbls = np.concatenate((rch,gch,bch), axis=2).astype(np.single);
                        imgB = img; b = self.image_size/2-osz/2; slc = slice(b,b+osz); imgB[slc,slc,:] = imglbls;
                        pl.imshow(imgB.transpose((1,0,2)),interpolation=interp_string);
                    pl.title('label, imgno %d' % imgno)
                    
                    #alpha = 0.6 # overlay segmented labels with data
                    pl.subplot(2,2,2*i+2)
                    seglbls = seglabels[:,imgno].reshape((self.seg_out_size,self.seg_out_size))
                    print seglbls
                    pl.imshow(imgA.transpose((1,0,2)),interpolation=interp_string); pl.hold(True)
                    m = pl.cm.ScalarMappable(norm=plt.colors.Normalize(), cmap=pl.cm.jet)
                    imgseg = m.to_rgba(seglbls % 256); imgseg[:,:,3] = alpha; imgseg[seglbls==0,3] = 0
                    img2 = np.zeros((self.image_size, self.image_size, 4), dtype=np.single, order='C')
                    b = self.image_size/2-self.seg_out_size/2; slc = slice(b,b+self.seg_out_size)
                    img2[slc,slc,:] = imgseg;
                    pl.imshow(img2.transpose((1,0,2)),interpolation=interp_string)
                    pl.title('seglabel')
            pl.show()

    # simpler plotting for just data, useful for debugging preprocessing for autoencoders
    def plotData(self,data,dataProc,pRand=True,image_size=0):
        if image_size < 1: image_size = self.image_size
        imgno = -1; interp_string = 'nearest' # 'none' not supported by slightly older version of matplotlib (meh)
        numpix = self.image_size*self.image_size
        # just keep bring up plots with EM data sample from batch range
        while True:
            pl.figure(1);
            # original mode, softmax labels for single output pixel
            for i in range(2):
                imgno = random.randrange(self.num_cases_per_batch) if pRand else imgno+1
                pl.subplot(2,2,2*i+1)
                slc = data[:,imgno].reshape(self.nzslices, image_size, image_size)[self.nzslices/2,:,:].\
                    reshape(image_size, image_size)
                mx = slc.max(); mn = slc.min(); fc = np.isfinite(slc).sum()
                h = pl.imshow(slc.transpose((1,0)),interpolation=interp_string); h.set_cmap('gray')
                pl.title('orig imgno %d, min %.2f, max %.2f, naninf %d' % (imgno, mn, mx, numpix - fc))
                pl.subplot(2,2,2*i+2)
                slc = dataProc[:,imgno].reshape(self.nzslices, self.image_out_size, 
                    self.image_out_size)[self.nzslices/2,:,:].reshape(self.image_out_size, self.image_out_size)
                mx = slc.max(); mn = slc.min(); fc = np.isfinite(slc).sum()
                h = pl.imshow(slc.transpose((1,0)),interpolation=interp_string); h.set_cmap('gray')
                pl.title('preproc imgno %d, min %.2f, max %.2f, naninf %d' % (imgno, mn, mx, numpix - fc))
            pl.show()

    '''
    # moved preprocessing into convnet for the most part, so these methods are maybe obsolete?
    #   keeping this here incase, at least scalar mean and std are potentially useful.
    def preprocessData(self, d, doSetup=False, reScale=False):
        d = d.astype(self.PDTYPE)   # do all preprocessing as same type (double)
        if reScale:
            dmean = d.mean(0,keepdims=True,dtype=self.PDTYPE); dstd = d.std(0,keepdims=True,dtype=self.PDTYPE)

        if any(self.em_standard_filter):
            #EMDataParser.emStandardFilterProc(self, d, reScale=False)
            self.emStandardFilter(d, reScale=False)    # threaded version
        if self.batch_meta['scalar_data_mean'] > 0:
            d -= self.batch_meta['scalar_data_mean']
        if self.batch_meta['scalar_data_std'] != 1:
            d /= self.batch_meta['scalar_data_std']
        if self.overall_normalize: 
            self.normalizeOverall(d, calcUS=doSetup)
        if self.pixel_normalize: 
            self.normalizePerPixel(d, calcUS=doSetup)
        if self.case_normalize: 
            EMDataParser.normalizePerCase(d)
        # if whiten starts with c that means the whitening is done in convnet, used for initializing whitening matrix
        if self.whiten and self.whiten != 'none' and self.whiten[0] != 'c': 
            d = self.whitening(d, calcW=doSetup, wtype=self.whiten, epsilon=self.whiten_epsilon)

        if d.size > 0 and reScale:
            dmean2 = d.mean(0,keepdims=True,dtype=self.PDTYPE); dstd2 = d.std(0,keepdims=True,dtype=self.PDTYPE)
            d = (d - dmean2)/dstd2*dstd + dmean
        return d.astype(np.single)  # return in convnet precision (single)

    def initPreprocessData(self):
        if not self.silent: print 'EMDataParser: Initializing batch data pre-process with whiten %s epslion %g, '\
            'scalar mean %.4f, scalar std %.4f, overall normalize %d, pixel normalize %d, case normalize %d, '\
            'em standard filter %d (%d %d) idelta (%.4f %.4f) iinterp %d gamma %.4f' % \
            (self.whiten, self.whiten_epsilon, self.batch_meta['scalar_data_mean'], self.batch_meta['scalar_data_std'], 
            self.overall_normalize, self.pixel_normalize, self.case_normalize, any(self.em_standard_filter), 
            self.em_standard_filter[0], self.em_standard_filter[1], self.em_standard_idelta[0], 
            self.em_standard_idelta[1], self.em_standard_iinterp, self.em_standard_gamma)
        if not self.silent: print '\tactual EM u=%.16g s=%.16g' % (self.EM_actual_mean, self.EM_actual_std)
        
        # optionally load the whitening matrix
        if self.whiten and self.whiten != 'none' and self.do_whiten_load:
            d = self.whitening(np.zeros((0,1),dtype=self.PDTYPE), calcW=True)

        # the init is slowish no matter what, so only do it if we have to
        self.anyInitPreProcess = (self.whiten and self.whiten != 'none' and self.whiten[0] != 'c' and \
            not self.do_whiten_load) or self.pixel_normalize or self.overall_normalize
        if not self.anyInitPreProcess: 
            if not self.silent: print '\tno batch pre-processing required'
            return
        # xxx - this causes a infinite recursion, decided not to fix because migrating to pre-processing in convnet
        assert( not self.use_chunk_list )   # preproc not currently allowed with chunklist mode
    
        t = time.time()
        preproc_data = np.zeros((self.pixels_per_image,0), dtype=np.single, order='C')
        start_batch = self.FIRST_RAND_NOLOOKUP_BATCH if self.no_labels else 1
        if not self.silent: 
            print '\tpre-processing based on %d rand batches starting at %d' % (self.preproc_nbatches, start_batch)
        for i in range(start_batch,start_batch+self.preproc_nbatches): 
            data, labels = self.getBatch(i, do_preprocess=False)
            preproc_data = np.concatenate((preproc_data,data),1)
        self.preprocessData(preproc_data, doSetup=True)
        print '\tdone with batch pre-processing init in %.3f s' % (time.time()-t,)
        
        if self.overall_normalize:
            if not self.silent: print '\toverall u=%.16g s=%.16g' % (self.X_overall_mean, self.X_overall_std)

    def initWhitenData(self, feature_path, batch_range):
        plot_smoothed = False
        plot_whitened = False
        
        nbatches = len(batch_range); cpb = self.num_cases_per_batch; ntotal = cpb*nbatches
        print 'convnet pre-processing based on %d batches starting at %d with whiten %s epsilon %g' % (nbatches, 
            batch_range[0], self.whiten, self.whiten_epsilon)
        # have to specify convnet whitening mode for write_features_type 'data'
        assert( self.whiten != 'none' and self.whiten[0]=='c')  
        t = time.time()
        d = np.zeros((self.pixels_per_out_image, ntotal), dtype=np.single, order='C')
        for i,batchnum in zip(range(0,ntotal,cpb),batch_range):
            # xxx - transpose or not depends on if feature layer is transpose layer.... assuming transpose layer here
            d[:,i:i+cpb] = np.load(os.path.join(feature_path,'data_batch_data_%d.npy' % batchnum)).T
            ##d['labels'] = np.load(os.path.join(feature_path,'data_batch_lbls_%d.npy' % batchnum)) # no labels needed
            if plot_smoothed:
                data = np.load(os.path.join(feature_path,'data_batch_%d.npy' % batchnum))
                self.plotData(data,d[:,i:i+cpb],False)
            # batches take up way too make space and won't need these again so remove
            os.remove(os.path.join(feature_path,'data_batch_data_%d.npy' % batchnum)) 
            os.remove(os.path.join(feature_path,'data_batch_lbls_%d.npy' % batchnum)) 
            os.remove(os.path.join(feature_path,'data_batch_%d.npy' % batchnum)) 
        print '\tloaded %d total examples from %d batches with %d cases per batch in %.3f s, whitening' % (ntotal, 
            nbatches, cpb, time.time()-t)

        t = time.time()
        d = d.astype(self.PDTYPE)   # do all preprocessing as same type (double)
        # sample overall mean and std are supplied as parameters to whitening module to apply before whitening
        amean = d.mean(); astd = d.std(); print '\tsampled EM u=%.20g s=%.20g' % (amean, astd)
        if plot_whitened:
            dmean = d.mean(0,keepdims=True); dstd = d.std(0,keepdims=True); dorig = d
        d -= amean; d /= astd   # subtract mean and divide std before whitening
        d = self.whitening(d, calcW=True, wtype=self.whiten[1:], epsilon=self.whiten_epsilon, whiten=plot_whitened)
        if plot_whitened:
            dmean2 = d.mean(0,keepdims=True); dstd2 = d.std(0,keepdims=True)
            d = ((d - dmean2)/dstd2*dstd + dmean).astype(np.single)
            self.plotData(dorig,d,False,image_size=self.image_out_size)
        print '\twhitened in %.3f s' % (time.time()-t,)

    # see "Learning Multiple Layers of Features from Tiny Images" Krizhevsky 2009
    # https://gist.github.com/duschendestroyer/5170087
    # http://ufldl.stanford.edu/wiki/index.php/Implementing_PCA/Whitening
    def whitening(self, X, epsilon=1e-7, calcW=False, wtype='zca', whiten=False):
        if calcW:
            if self.do_whiten_load:
                print '\tloading %s whitening matrix from %s' % (wtype,self.whiten_load)
                self.W_whiten = np.fromfile(self.whiten_load,dtype=X.dtype).\
                    reshape((self.pixels_per_image,self.pixels_per_image))
            else:
                sigma = np.dot(X,X.T) / (X.shape[1] - 1); U, S, V = linalg.svd(sigma,overwrite_a=True)
                if wtype == 'zca':
                    self.W_whiten = np.dot(np.dot(U, np.diag(1/np.sqrt(S+epsilon))), U.T)
                elif wtype == 'pca':
                    self.W_whiten = np.dot(np.diag(1/np.sqrt(S+epsilon)), U.T)
                else:
                    assert(False)
                if self.save_name:
                    if os.path.isdir(self.whiten_load):
                        fn = os.path.join(self.whiten_load, self.save_name + '_W_whiten.raw')
                    else:
                        # just save to cwd if path to save not specified
                        fn = self.save_name + '_W_whiten.raw'
                    print '\tsaving %s whitening matrix to %s' % (wtype,fn)
                    self.W_whiten.tofile(fn)
            # for speed / memory, don't whiten and return nothing when calc'ing whitening matrix
            Xr = np.zeros((0,))
        if not calcW or whiten:
            Xr = np.dot(self.W_whiten, X)
        return Xr

    # subtract overall mean and divide variance in place
    def normalizeOverall(self, X, calcUS=False):
        if calcUS:
            self.X_overall_mean = X.mean(dtype=self.PDTYPE)
            self.X_overall_std = X.std(dtype=self.PDTYPE)
        X -= self.X_overall_mean; X /= self.X_overall_std;

    # subtract pixel mean and divide variance in place
    def normalizePerPixel(self, X, calcUS=False):
        if calcUS:
            self.X_pixel_mean = X.mean(1,keepdims=True,dtype=self.PDTYPE)
            self.X_pixel_std = X.std(1,keepdims=True,dtype=self.PDTYPE)
        X -= self.X_pixel_mean; X /= self.X_pixel_std;

    # easy parallelization of raw em standard preprocessing method (each image processed independently)
    def emStandardFilter(self, X, reScale=False):
        threads = self.NUM_FILTER_THREADS * [None]
        assert( X.shape[1] % self.NUM_FILTER_THREADS == 0 ) # xxx - currently not dealing with remainders
        imgs_per_thread = X.shape[1] / self.NUM_FILTER_THREADS
        for i in range(self.NUM_FILTER_THREADS):
            threads[i] = EMStandardFilterThread(self, X[:,i*imgs_per_thread:(i+1)*imgs_per_thread], reScale)
            threads[i].start()
        for i in range(self.NUM_FILTER_THREADS):
            threads[i].join()

    # standard method of preprocessing raw EM data (minus contrast enhancement)
    # code adapted from preprocessing raw EM for frontend in lbPreprocessRawEM.py
    # upsample data, filter, downsample back to original sampling, optionally return to original mean/std per image
    # xxx - this is slowish, so kindof limited what we can do here; could instead apply this to whole dataset, 
    #   but this is super slow (~1 hour for 27 cubes), so would have to save preprocessed data to be practical.
    @staticmethod
    def emStandardFilterProc(dp, X, reScale=False):
        # save the original mean and std per image, reshape into images
        if reScale:
            dmean = X.mean(0,keepdims=True,dtype=dp.PDTYPE); dstd = X.std(0,keepdims=True,dtype=dp.PDTYPE)
        sorig = X.shape; s = (dp.image_size,dp.image_size,X.shape[1]); X = X.reshape(s)
        
        x = np.arange(s[0], dtype=dp.PDTYPE); y = np.arange(s[1], dtype=dp.PDTYPE); d = dp.em_standard_idelta
        xi = np.arange(0, s[0], d[0], dtype=dp.PDTYPE); yi = np.arange(0, s[1], d[1], dtype=dp.PDTYPE)
        #W = np.ones(dp.em_standard_filter, dtype=dp.PDTYPE) / dp.em_standard_filter.prod()
        for zind in range(s[2]):
            #t = time.time()
            z = X[:,:,zind].reshape((s[0],s[1]))
            f = interpolate.interp2d(x, y, z, kind='linear', copy=False)
            # xxx - add an option for the filter type?
            #zi = filters.convolve(f(xi,yi), W, mode='reflect', cval=0.0, origin=0)
            zi = filters.median_filter(f(xi,yi), size=dp.em_standard_filter, mode='reflect', cval=0.0)
            if dp.em_standard_iinterp:
                X[:,:,zind] = zi[0:-1:dp.em_standard_interpn[0],0:-1:dp.em_standard_interpn[1]]
            else:
                assert(False) # not recommended, much slower
                f = interpolate.interp2d(xi, yi, zi, kind='linear', copy=False)
                X[:,:,zind] = f(x,y)
            #print '%.5f s' % (time.time() - t,)

        if dp.em_standard_gamma != 1: np.power(X, dp.em_standard_gamma, out=X)
        
        # return back to original shape and mean/std per image (case)
        X = X.reshape(sorig);
        if reScale:
            dmean2 = X.mean(0,keepdims=True,dtype=dp.PDTYPE); dstd2 = X.std(0,keepdims=True,dtype=dp.PDTYPE)
            X = (X - dmean2)/dstd2*dstd + dmean

    # subtract example mean and divide variance in place
    @staticmethod
    def normalizePerCase(X):
        Xmean = X.mean(0,keepdims=True, dtype=self.PDTYPE); Xstd = X.std(0,keepdims=True, dtype=self.PDTYPE)
        X -= Xmean; X /= Xstd;
    '''

    @staticmethod
    def augmentData(d,augment):
        if augment == 0: return d                               # no augmentation
        if np.bitwise_and(augment,4): d = d.transpose(1,0,2)    # tranpose x/y
        if np.bitwise_and(augment,1): d = d[::-1,:,:]           # reflect x
        if np.bitwise_and(augment,2): d = d[:,::-1,:]           # reflect y
        if np.bitwise_and(augment,8): d = d[:,:,::-1]           # reflect z
        return d

    @staticmethod
    def get_options(cfg_file):
        config = ConfigObj(cfg_file, 
            configspec=os.path.join(os.path.dirname(os.path.realpath(__file__)),'parseEMdataspec.ini'))

        # Validator handles missing / type / range checking
        validator = Validator()
        results = config.validate(validator, preserve_errors=True)
        if results != True:
            for (section_list, key, err) in flatten_errors(config, results):
                if key is not None:
                    if not err:
                        print 'EMDataParser: The "%s" key is missing in the following section(s):%s ' \
                            % (key, ', '.join(section_list))
                        raise ValidateError
                    else:
                        print 'EMDataParser: The "%s" key in the section(s) "%s" failed validation' \
                            % (key, ', '.join(section_list))
                        raise err
                elif section_list:
                    print 'EMDataParser: The following section(s) was missing:%s ' % ', '.join(section_list)
                    raise ValidateError
                    
        return config

    # xxx - moved logic out of convEMdata.py for better modularity, maybe can clean up more?
    def checkOutputCubes(self, feature_path, batchnum, isLastbatch):
        if self.use_chunk_list:
            # decide if it's appropriate to make output cubes (if at the end of the current chunk)
            self.chunklistOutputCubes(feature_path, batchnum, isLastbatch)
        elif isLastbatch:
            # not chunklist mode, only write after all batches completed
            self.makeOutputCubes(feature_path)

    # special makeOutputCubes call for chunklist mode, only write if this is the last batch (overall or chunk)
    def chunklistOutputCubes(self, feature_path, batchnum, isLastbatch):
        assert( self.use_chunk_list )   # do not call me unless chunklist mode

        batchOffset, chunk = self.getTiledBatchOffset(batchnum, setChunkList=False)

        # write the output cubes if this is the last batch in current chunk or if this is the last overall batch
        if isLastbatch or (batchOffset == (self.batches_per_rand_cube - 1)):
            self.makeOutputCubes(feature_path, chunk*self.batches_per_rand_cube + self.FIRST_TILED_BATCH)
            # prevents last chunk from being written twice (for isLastbatch, next chunk might not have loaded)
            self.last_chunk_rand = self.chunk_rand; self.last_offset_rand = self.offset_rand; 

    # the EM data "unpackager", recreate probablity cubes using exported output features from convnet
    def makeOutputCubes(self, feature_path='', batchnum=-1):
        print 'EMDataParser: Loading exported features'
        cpb = self.num_cases_per_batch; size = self.image_out_size; 
        npix = self.pixels_per_out_image; nout = self.noutputs;

        # labels in this context are the labels per output pixel
        if self.independent_labels: nlabels = self.nIndepLabels; label_names = self.indep_label_names
        else: nlabels = self.nlabels; label_names = self.label_names
        
        # allow the starting batch to be passed in (for chunklist mode)
        if batchnum < 0: batchnum = self.FIRST_TILED_BATCH
        if self.verbose: 
            print 'ntiles_per_zslice %d zslices_per_batch %d tiled shape %d %d cpb %d' % (self.batches_per_zslice,
                self.zslices_per_batch, self.inds_tiled_out.shape[0],self.inds_tiled_out.shape[1], cpb) 

        # initial shape of probs out depends on single or multiple output pixels
        if size > 1: probs_out_shape = self.output_size + [nout]
        else: probs_out_shape = self.labels_slice_size + (nlabels,)
                
        # allocate the outputs to be written to hdf5, any pixels from missing batches are filled with EMPTY_PROB
        if hasattr(self, 'probs_out'):
            # do not reallocate in chunklist mode, but reshape (shape changes below for multiple output pixels)
            self.probs_out[:] = self.EMPTY_PROB
            self.probs_out = self.probs_out.reshape(probs_out_shape)
        else:
            self.probs_out = self.EMPTY_PROB * np.ones(probs_out_shape, dtype=np.float32, order='C')

        # get training prior if present in the meta
        if 'prior_train_count' in self.batch_meta:
            # calculate the training prior based on the actual labels that have been presented to the network
            prior_train = self.batch_meta['prior_train_count'] / self.batch_meta['prior_total_count'].astype(np.double)

        # if the training prior counts have been saved and the test prior is specified in the ini,
        #   then enable prior rebalancing on the output probabilities.
        prior_export = self.prior_test.all() and 'prior_train_count' in self.batch_meta
        if prior_export:
            # make sure the test (export) prior is legit
            assert( (self.prior_test > 0).all() and (self.prior_test < 1).all() )   # test priors must be probs
            # only for independent labels with independent prior test can test prior not sum to 1
            if not self.independent_labels or not self.prior_test_indep: assert( self.prior_test.sum() == 1 )    

            if not self.independent_labels or self.prior_test_indep or (self.prior_test.size == nlabels):
                # normal case, test_priors are for labels or independent labels
                assert( self.prior_test.size == nlabels )   # test priors must be for labels or independent label types
                if self.independent_labels:
                    # repeat prior_test for all output pixels
                    prior_test = self.prior_test.reshape((1,-1)).repeat(npix, axis=0).reshape((nout,))
                    if self.prior_test_indep:
                        # in this case each output is independently bayesian reweighted against the not output
                        prior_nottest_to_nottrain = (1 - prior_test) / (1 - prior_train)
                else:
                    prior_test = self.prior_test
            else:
                # allow the last class to be encoded as all zeros, so prob is 1-sum others
                assert( self.prior_test.size == nlabels+1 )
                noutp = npix*(nlabels+1)
                prior_test = self.prior_test.reshape((1,-1)).repeat(npix, axis=0).reshape((noutp,))
                prior_test_labels = self.prior_test[0:-1].reshape((1,-1)).repeat(npix, axis=0).reshape((nout,))
                prior_train_labels = prior_train
                prior_test_to_train_labels = (prior_test_labels / prior_train_labels).reshape((1,size,size,nlabels))
                ptall = prior_train.reshape((size,size,nlabels))
                prior_train = np.concatenate((ptall, 1-ptall.sum(axis=2,keepdims=True)), axis=2).reshape((noutp,))

            # calculate ratio once here to avoid doing it every loop iteration below
            prior_test_to_train = prior_test / prior_train
            if self.independent_labels and not self.prior_test_indep:
                if self.prior_test.size == nlabels:
                    prior_test_to_train = prior_test_to_train.reshape((1,size,size,nlabels))
                else:
                    prior_test_to_train = prior_test_to_train.reshape((1,size,size,nlabels+1))
    
        # load the pickled output batches and assign based on tiled indices created in packager (makeTiledIndices)
        for z in range(0,self.ntotal_zslice,self.zslices_per_batch):
            for t in range(self.batches_per_zslice):
                batchfn = os.path.join(feature_path,'data_batch_%d' % batchnum)
                if os.path.isfile(batchfn):
                    infile = open(batchfn, 'rb'); d = myPickle.load(infile); infile.close(); d = d['data']
                    # batches take up way too make space for "large dumps" so remove them in append_features mode
                    if self.append_features: os.remove(batchfn)
                    if prior_export:
                        # apply Bayesian reweighting, either independently or over the labels set
                        if self.independent_labels and self.prior_test_indep:
                            # sum is with the not target for independent outputs
                            adjusted = d*prior_test_to_train; d = adjusted / (adjusted+(1-d)*prior_nottest_to_nottrain)
                        elif self.independent_labels:
                            if self.prior_test.size != nlabels:
                                # need 1 - sum for last label type (encoded as all zeros)
                                dshp = d.reshape((cpb,size,size,nlabels))
                                dall = np.concatenate((dshp, 1-dshp.sum(axis=3,keepdims=True)),axis=3)
                                d = (dshp*prior_test_to_train_labels / (dall*prior_test_to_train).sum(axis=3,
                                    keepdims=True)).reshape((cpb,nout))
                            else:
                                dshp = d.reshape((cpb,size,size,nlabels)); adjusted = dshp*prior_test_to_train;
                                d = (adjusted / adjusted.sum(axis=3,keepdims=True)).reshape((cpb,nout))
                        else:
                            # sum is over the labels
                            adjusted = d*prior_test_to_train; d = adjusted / adjusted.sum(axis=1,keepdims=True)
                    begr = t*cpb; endr = begr + cpb
                    self.probs_out[self.inds_tiled_out[begr:endr,0],self.inds_tiled_out[begr:endr,1],
                        self.inds_tiled_out[begr:endr,2] + z,:] = d
                batchnum += 1

        if size > 1:
            # xxx - oh yah, this makes sense, see comments in makeTiledIndices
            self.probs_out = self.probs_out.reshape(self.output_size + [size, size, 
                nlabels]).transpose((0,3,1,4,2,5)).reshape(self.labels_slice_size + (nlabels,))

        if self.write_outputs:
            print 'EMDataParser: Creating hdf5 output containing label probabilities'
            if not os.path.exists(self.outpath): os.makedirs(self.outpath)
            # write probs in F-order, use separate variable names in hdf file
            outfile = h5py.File(os.path.join(self.outpath, self.OUTPUT_H5_CVOUT), 'w'); 

            # output probability for each output if requested
            for n in range(nlabels):
                outfile.create_dataset(label_names[n], data=self.probs_out[:,:,:,n].transpose((2,1,0)),
                    compression='gzip', compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
                # copy any attributes over
                for name,value in self.data_attrs.items():
                    outfile[label_names[n]].attrs.create(name,value)
            
        if self.append_features:
            # write outputs probabilities to a big hdf5 that spans entire dataset, used for "large feature dumps".
            # always writes in F-order (inputs can be either order tho)
            assert( self.nz_tiled == 0 ) # use the rand cube only for "large feature dumps"
            hdf = h5py.File(self.imagesrc,'r'); createDataset = False
            if not os.path.isfile(self.outpath):
                createDataset = True
                print 'EMDataParser: Creating global hdf5 output containing label probabilities "%s"' % self.outpath
                # create an output prob hdf5 file (likely for a larger dataset, this is how outputs are "chunked")
                outfile = h5py.File(self.outpath, 'w'); 
                for n in range(nlabels):
                    # get the shape and chunk size from the data hdf5. if this file is in F-order, re-order to C-order 
                    shape = list(hdf[self.dataset].shape); chunks = list(hdf[self.dataset].chunks)
                    if not self.hdf5_Corder:
                        shape = shape[::-1]; chunks = chunks[::-1]
                    # now re-order the dims based on the specified re-ordering and then re-order back to F-order
                    shape = list(shape[i] for i in self.zreslice_dim_ordering)
                    chunks = list(chunks[i] for i in self.zreslice_dim_ordering)
                    shape = shape[::-1]; chunks = tuple(chunks[::-1])
                    
                    outfile.create_dataset(label_names[n], shape=shape, dtype=np.float32, compression='gzip', 
                        compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True, fillvalue=-1.0, chunks=chunks)
                    # copy the attributes over
                    for name,value in self.data_attrs.items():
                        outfile[label_names[n]].attrs.create(name,value)
                outfile.close()
                
            print 'EMDataParser: Appending to global hdf5 output containing label probabilities "%s" at %d %d %d' % \
                (self.outpath, self.last_chunk_rand[0], self.last_chunk_rand[1], self.last_chunk_rand[2])
            outfile = h5py.File(self.outpath, 'r+'); 
            for n in range(nlabels):
                # always write outputs in F-order
                ind = self.get_hdf_index_from_chunk_index(hdf[self.dataset], self.last_chunk_rand, 
                    self.last_offset_rand)
                ind = ind[self.zreslice_dim_ordering][::-1] # re-order for specified ordering, then to F-order
                d = self.probs_out[:,:,:,n].transpose((2,1,0)); dset = outfile[label_names[n]]
                #print ind, d.shape, dset.shape
                dset[ind[0]:ind[0]+d.shape[0],ind[1]:ind[1]+d.shape[1],ind[2]:ind[2]+d.shape[2]] = d
            hdf.close()
        
        # for both modes, write out the priors, if prior reweighting enabled
        # write a new dataset with the on-the-fly calculated training prior for each label type
        if 'prior_train_count' in self.batch_meta and (not self.append_features or createDataset):
            if not prior_export or self.prior_test.size == nlabels:
                d = prior_train.reshape((size,size,nlabels))
            else:
                d = prior_train_labels.reshape((size,size,nlabels))
            outfile.create_dataset(self.PRIOR_DATASET, data=d.transpose((2,1,0)),
                compression='gzip', compression_opts=self.HDF5_CLVL, shuffle=True, fletcher32=True)
            if prior_export:
                print 'EMDataParser: Exported with Bayesian prior reweighting'
                outfile[self.PRIOR_DATASET].attrs.create('prior_test',self.prior_test)
            else:
                print 'EMDataParser: Exported training prior but output not reweighted'

        outfile.close()
            

# for test
if __name__ == '__main__':
    dp = EMDataParser(sys.argv[1:][0], True)
    #dp = EMDataParser(sys.argv[1:][0], False, '', 'meh1')
    #dp.no_label_lookup = True
    dp.initBatches()

    #dp.makeOutputCubes(sys.argv[1:][1])

    nBatches = 10;
    # test rand batches
    #for i in range(nBatches): dp.getBatch(i+1, True)
    for i in range(dp.FIRST_RAND_NOLOOKUP_BATCH,dp.FIRST_RAND_NOLOOKUP_BATCH+nBatches): dp.getBatch(i+1, True)
    # test tiled batches
    batchOffset = 0;
    for i in range(dp.FIRST_TILED_BATCH+batchOffset,dp.FIRST_TILED_BATCH+batchOffset+nBatches): dp.getBatch(i,True,0)
    
