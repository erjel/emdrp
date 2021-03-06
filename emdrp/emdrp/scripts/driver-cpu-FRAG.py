#!/usr/bin/env python
# use python3

import numpy as np
import time

#from dpLoadh5 import dpLoadh5
from dpFRAG import dpFRAG

# labeled chunks
#chunk_range_beg     = 17,19,2, 17,23,1, 22,23,1, 22,18,1, 22,23,2, 19,22,2
chunk = [17,19,2]
size = [128,128,128]
offset = [0,0,0]
has_ECS = True

#username = 'watkinspv'
username = 'patilra'

# Input supervoxel labels (hdf5)
labelfile           = '/Data/' + username + '/full_datasets/neon_sixfold/mbfergus32/huge_supervoxels.h5'
label_subgroups     = ['with_background','0.99999000']

# Input probability data (hdf5)
probfile            = '/Data/' + username + '/full_datasets/neon_sixfold/mbfergus32/huge_probs.h5'

# Input segmented labels (hdf5)
gtfile              = '/Data/datasets/labels/gt/M0007_33_labels_briggmankl_watkinspv_39x35x7chunks_Forder.h5'
gt_dataset          = 'labels'

# Input raw EM data
rawfile             = '/Data/datasets/raw/M0007_33_39x35x7chunks_Forder.h5'
raw_dataset         = 'data_mag1'

# Output agglomerated labels
outfile             = '/Data/' + username + '/tmp_agglo_out.h5'

# Input probability augmented data
probaugfile         = ''
#probaugfile         = '/Data/' + username + '/full_datasets/neon_sixfold/mbfergus32/huge_probs.h5'

# Input raw EM augmented data
rawaugfile          = ''
#rawaugfile          = '/Data/datasets/raw/M0007_33_39x35x7chunks_Forder_aug.h5'

# output raw supervoxels (with empty labels removed)
rawout              = '/home/' + username + ('/Downloads/svox_%dx%dx%d.raw' % tuple(size))

feature_set = 'minimal'
progressBar = True
verbose = True

# use getFeatures=False to only get the RAG (wihtout boundary voxels or features)
getFeatures = False

# instantiate frag and load data
frag = dpFRAG.makeBothFRAG(labelfile, chunk, size, offset,
    [probfile, probaugfile], [rawfile, rawaugfile],
    raw_dataset, gtfile, outfile, label_subgroups, ['training','thr'],
    progressBar=progressBar, feature_set=feature_set, has_ECS=has_ECS,
    verbose=verbose)
# hack to save raveled indices of overlap in context of whole volume (including boundary)
# boundary size is saved in frag.eperim
frag.ovlp_attrs += ['ovlp_cur_dilate']
# create graph
frag.createFRAG(features=getFeatures)

# just to use same name for RAG networkx object as was in driver-cpu.py (from gala example.py)
g_train = frag.FRAG

# save adjacency matrix
print('Exporting adjacency matrix'); t=time.time()
import networkx as nx
am=nx.to_numpy_matrix(g_train)
#np.savetxt("tmp-adjacency_matrix-cpu.txt",am, fmt="%d", delimiter='')
print(am.dtype, am.shape)
fn = 'tmp-adjacency-matrix-cpu-%dx%d-%s.raw' % (am.shape[0], am.shape[1], str(am.dtype))
am.tofile(fn)
am2 = np.fromfile(fn, dtype=np.float64).reshape(am.shape)
print(am2.dtype, am2.shape)
print('\tdone in %.4f s' % (time.time() - t))

# dump supervoxels
frag.supervoxels_noperim.transpose((2,1,0)).tofile(rawout)

if getFeatures:
    print('Outputting boundary voxels'); t=time.time()
    fout = open("tmp-boundary_pixel_indices-cpu.txt","w")
    edges = g_train.edges()
    edges.sort()
    for edge in edges:
        fout.write("(%d, %d): "%(edge[0],edge[1]))
        #for b in g_train[edge[0]][edge[1]]['boundary']:
        #    fout.write("%d "%b)
        boundary_subs = np.transpose(np.nonzero(g_train[edge[0]][edge[1]]['ovlp_attrs']['ovlp_cur_dilate']))
        start_sub = np.array([x.start for x in g_train[edge[0]][edge[1]]['ovlp_attrs']['aobnd']])
        #global_subs_padded = boundary_subs + start_sub
        #global_inds = np.ravel_multi_index(global_subs_padded.T.reshape(3,-1), frag.supervoxels.shape)
        #for b in global_inds:
        #    fout.write("%d "%b)
        global_subs_unpadded = boundary_subs + start_sub - frag.eperim
        for b in range(global_subs_unpadded.shape[0]):
            fout.write("(%d,%d,%d) " % tuple(global_subs_unpadded[b,:].tolist()))
        fout.write("\n")
    fout.close()
    print('\tdone in %.4f s' % (time.time() - t))
