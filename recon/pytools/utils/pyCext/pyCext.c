
/* C extensions for python using numpy for processing EM data.
 * Structure based on:
 *      http://wiki.scipy.org/Cookbook/C_Extensions/NumPy_arrays
 *
 * Created pwatkins 14 Apr 2015
 *
 *
 */

#include "Python.h"
#define NPY_NO_DEPRECATED_API NPY_1_10_API_VERSION
#include "arrayobject.h"
#include "pyCext.h"

/* #### Globals #################################### */

/* ==== Set up the methods table ====================== */
static PyMethodDef _pyCextMethods[] = {
    // EM data extensions
    {"label_affinities", label_affinities, METH_VARARGS},
    {"binary_warping", binary_warping, METH_VARARGS},
    {"merge_supervoxels", merge_supervoxels, METH_VARARGS},
    {"type_components", type_components, METH_VARARGS},

    {NULL, NULL}     /* Sentinel - marks the end of this structure */
};


/* ==== Initialize the C_test functions ====================== */
// Module name must be _pyCext in compile and linked 

// https://docs.python.org/3.3/howto/cporting.html
// http://python3porting.com/cextensions.html

#if PY_MAJOR_VERSION >= 3
static struct PyModuleDef moduledef = {
        PyModuleDef_HEAD_INIT,
        "_pyCext",           /* m_name */
        NULL,                /* m_doc */
        -1,                  /* m_size */
        _pyCextMethods,      /* m_methods */
        NULL,                /* m_reload */
        NULL,                /* m_traverse */
        NULL,                /* m_clear */
        NULL                 /* m_free */
};

PyObject* PyInit__pyCext(void) 
#else
void init_pyCext()  
#endif
{

#if PY_MAJOR_VERSION >= 3
    PyObject *module = PyModule_Create(&moduledef);
#else
    (void) Py_InitModule("_pyCext", _pyCextMethods);
    //PyObject *module = Py_InitModule("myextension", myextension_methods);
#endif

    import_array();  // Must be present for NumPy.  Called first after above line.

#if PY_MAJOR_VERSION >= 3
    return module;
#endif
}

/* #### Extended modules for EM #################################### */

/* Had to write specialized connected components to use the affinities in each direction
 *   instead of pixel connectivities. This is the simplest method, "component-by-component" labeling using a stack.
 */
static PyObject *label_affinities(PyObject *self, PyObject *args)
{
    PyArrayObject *affinities, *labels;
    float *affs, threshold;
    npy_uint32 *lbls, curlab;
    npy_int m,n,z,nd, i,j,k,nb, csub[LBLS_ND], nsub[LBLS_ND], N=LBLS_ND;
    npy_intp *dims, size, ind, nind, ptr=0, cursize;
    //npy_intp dimslbls[LBLS_ND];
    npy_uint16 *stack[LBLS_ND];
    
    /* Inputs are affinity graph ndarray and threshold (single float) */
    // take the labels as input, so that this process can be repeated with different thresholds.
    // if a voxel has already been previously labeled, it will not be reassigned.
    // curlab is also taken as input as to the label to start with (usually one plus the max in the input labels)
    if (!PyArg_ParseTuple(args, "O!O!If", &PyArray_Type, &affinities, &PyArray_Type, &labels, &curlab, &threshold)) 
        return NULL;
    if (NULL == affinities) return NULL;
    
    /* Get the dimensions of the labels and the c pointer to the affinities and labels */
    dims = PyArray_DIMS(affinities); m = dims[0]; n = dims[1]; z = dims[2]; nd = dims[3]; size = m*n*z;
    affs = (float *) PyArray_DATA(affinities);
    //dimslbls[0] = m; dimslbls[1] = n; dimslbls[2] = z;     
    //labels=(PyArrayObject *) PyArray_SimpleNew(nd,dimslbls,NPY_UINT32);
    lbls = (npy_uint32 *) PyArray_DATA(labels);
    
    /* Allocate the stack, allocate worst case which is all voxels. */
    // Assume no volume dimension is larger than 16 bit (safe assumption given memory required for larger volumes)
    for( i=0; i<LBLS_ND; i++ ) {
        stack[i] = (npy_uint16 *) malloc((size_t) (size*sizeof(npy_uint16)));
        if( stack[i] == NULL ) {
            printf("In label_affinities allocation of memory for stack failed."); exit(0);
        }
    }
    
    /* http://en.wikipedia.org/wiki/Connected-component_labeling
    It is assumed that the input image is a binary image, with pixels being either background or foreground and that 
    the connected components in the foreground pixels are desired. The algorithm steps can be written as:

    (1) Start from the first pixel in the image. Set "curlab" (short for "current label") to 1. Go to (2).
    (2) If this pixel is a foreground pixel and it is not already labelled, then give it the label "curlab" and 
        add it as the first element in a queue, then go to (3). If it is a background pixel, then repeat (2) for the 
        next pixel in the image.
    (3) Pop out an element from the queue, and look at its neighbours (based on any type of connectivity). If a 
        neighbour is a foreground pixel and is not already labelled, give it the "curlab" label and add it to the queue. 
        Repeat (3) until there are no more elements in the queue.
    (4) Go to (2) for the next pixel in the image and increment "curlab" by 1.
    
    This basic algorithm is used, but instead of using the voxels for connectivity, a threshold on the affinity graph
      is used instead to determine connectivity in each direction (3 dimension volume assumed in this code).
    */

    // outer loops iterate over all voxels (again, 3d volume is assumed for simplicity)
    for( i=0; i<m; i++ ) {
        for( j=0; j<n; j++ ) {
            for( k=0; k<z; k++ ) {
                ind = i*n*z + j*z + k;  // single index into volume (C-order)
                
                if( !lbls[ind] ) {  // step (2)
                    lbls[ind] = curlab; cursize = 1;     // Not already labeled
                    stack[0][ptr] = i; stack[1][ptr] = j; stack[2][ptr] = k; ptr++;     // Push to stack

                    // step (3), fully label this component by looking at each neighbor
                    while( ptr > 0 ) {
                        ptr--; csub[0]=stack[0][ptr]; csub[1]=stack[1][ptr]; csub[2]=stack[2][ptr]; // Pop from stack
                        // Check all neighbors of this element, and label if connected
                        for( nb=0; nb<LBLS_ND; nb++ ) {
                            // check "down" in this dim
                            nsub[0] = csub[0]; nsub[1] = csub[1]; nsub[2] = csub[2]; nsub[nb]--;
                            if( nsub[nb] >= 0 ) {
                                nind = nsub[0]*n*z + nsub[1]*z + nsub[2];
                                // check if not already labeled and connected to current voxel popped from stack
                                if( !lbls[nind] && affs[nsub[0]*n*z*N + nsub[1]*z*N + nsub[2]*N + nb] > threshold ) {
                                    // label this neighbor as same object, increment size, push to stack
                                    lbls[nind] = curlab; cursize++;
                                    stack[0][ptr] = nsub[0]; stack[1][ptr] = nsub[1]; stack[2][ptr] = nsub[2]; ptr++; 
                                }
                            }
                            // check "up" in this dim
                            nsub[0] = csub[0]; nsub[1] = csub[1]; nsub[2] = csub[2]; nsub[nb]++;
                            if( nsub[nb] < dims[nb] ) {
                                nind = nsub[0]*n*z + nsub[1]*z + nsub[2];
                                // check if not already labeled and connected to current voxel popped from stack
                                if( !lbls[nind] && affs[csub[0]*n*z*N + csub[1]*z*N + csub[2]*N + nb] > threshold ) {
                                    // label this neighbor as same object, increment size, push to stack
                                    lbls[nind] = curlab; cursize++;
                                    stack[0][ptr] = nsub[0]; stack[1][ptr] = nsub[1]; stack[2][ptr] = nsub[2]; ptr++; 
                                }
                            }
                        } // for each dimension (hard-coded indices and loops for 3 dims, C-order)
                    } // while current component is being labeled (items in stack)

                    // step (4), plus do not label completely unconnected voxels (those with size 1)
                    // for affinity graph components, unconnected voxels are by definition background.
                    if( cursize > 1 ) curlab++;
                    else lbls[ind] = 0;
                } // if current voxel unlabeled
            } // for each dim2
        } // for each dim1
    } // for each dim0

    // clean up
    free(stack[0]); free(stack[1]); free(stack[2]);

    //return PyArray_Return(labels);
    // return the current last label assigned in the label volume
    return Py_BuildValue("I", curlab-1);
}


/* Perform 3d warping using fixed lookup table (LUT). Connectivity depends on supplied LUT.
 * Operates on binary images only (operations on labels are not well defined by digital topology).
 */
static PyObject *binary_warping(PyObject *self, PyObject *args)
{
    PyArrayObject *source, *target, *mask, *oSimpleLUT, *oNonSimple;
    npy_bool *src, *tgt, *msk, patch[27], *new_src = NULL, new_patch[27];
    npy_int m,n,nz,x,y,z;
    npy_intp numiters, iter = 0, diff = 0, diff_before = 0, i;
    npy_intp *dims, numel, *pts = NULL, *tmp_pts = NULL, pt;
    npy_uint32 ind, new_ind;
    npy_uint8 *simpleLUT, *nonSimple;
    int slow;
    // for gray scale data mode ("watershed")
    PyArrayObject *gray, *grayThresholds;
    npy_float32 *gry, *grayTs;
    npy_intp nGrayTs, gT, g;
    npy_bool *gry_msk = NULL, *cmsk;
    int useGray;
    
    // diff = _pyCext.binary_warping(src, tgt, msk, simpleLUT, gry, grayThresholds, nonSimple, numiters, slow)
    if (!PyArg_ParseTuple(args, "O!O!O!O!O!O!O!Li", &PyArray_Type, &source, &PyArray_Type, &target, 
            &PyArray_Type, &mask, &PyArray_Type, &oSimpleLUT, &PyArray_Type, &gray, &PyArray_Type, &grayThresholds, 
            &PyArray_Type, &oNonSimple, &numiters, &slow)) 
        return NULL;
    
    /* Get the dimensions of the labels and the c pointer to the labels and components */
    dims = PyArray_DIMS(source); m = dims[0]; n = dims[1]; nz = dims[2]; numel = PyArray_SIZE(source);
    src = (npy_bool *) PyArray_DATA(source);
    tgt = (npy_bool *) PyArray_DATA(target);
    msk = (npy_bool *) PyArray_DATA(mask);

    // added this method that allows binary warping to also act as a "watershed" based on gray scale image.
    // will only watershed to the point where topology is not violated (simple points).
    gry = (npy_float32 *) PyArray_DATA(gray);
    grayTs = (npy_float32 *) PyArray_DATA(grayThresholds);
    
    // simple LUT contains the classification of nonsimple points in a 3x3x3 neighborhood (patch)
    // the value identifies the type of nonsimple point, zero indicates that it is a simple point.
    // thus, code below uses !simpleLUT[ind] to identify simple points in the neighborhood patch.
    simpleLUT = (npy_uint8 *) PyArray_DATA(oSimpleLUT);
    nonSimple = (npy_uint8 *) PyArray_DATA(oNonSimple);

    // allocate the mismatching points
    pts = (npy_intp *) malloc((size_t) (numel*sizeof(npy_intp)));
    if( pts == NULL ) {
        printf("In binary_warping allocation of memory for pts failed."); exit(0);
    }

    // Slow mode only updates once per iteration, effectively only removing the perimeter pixels on each iteration.
    // This is useful for warping to a point, like the matlab 'shrink' in bwmorph, or to a skeleton in 3d
    if( slow ) {
        new_src = (npy_bool *) malloc((size_t) (numel*sizeof(npy_bool)));
        if( new_src == NULL ) {
            printf("In binary_warping allocation of memory for new_src failed."); exit(0);
        }
        memcpy( new_src, src, numel*sizeof(npy_bool) );
        
        // this is so misclassified points can be re-ordered, helps with better warps in slow (perimeter) mode.
        // on average does not give better warping errors in normal mode, so only enabled for slow mode.
        tmp_pts = (npy_intp *) malloc((size_t) (numel*sizeof(npy_intp)));
        if( tmp_pts == NULL ) {
            printf("In binary_warping allocation of memory for tmp_pts failed."); exit(0);
        }
    }

    nGrayTs = PyArray_SIZE(grayThresholds); useGray = (nGrayTs > 0);
    if( useGray ) {
        gry_msk = (npy_bool *) calloc((size_t) numel, sizeof(npy_bool));
        if( gry_msk == NULL ) {
            printf("In binary_warping allocation of memory for gry_msk failed."); exit(0);
        }
        cmsk = gry_msk;
    } else {
        nGrayTs = 1; cmsk = msk;
    }
    // this outer loop only serves a purpose when optionally using gray scale data and thresholds ("watershed")
    for( gT = 0; gT < nGrayTs; gT++ ) {
        if( useGray ) {
            // OR the curent mask with gray values greater than current threshold.
            // do not include any points that were not in the original mask.
            for( g = 0; g < numel; g++ ) if( !cmsk[g] && msk[g] && (gry[g] > grayTs[gT]) ) cmsk[g] = 1;
        }
        
        // this is the main body of the warping descent loop. 
        // flip each mismatching simple point in the order of mismatching points returned by get_misclass_points.
        // continue for a specified number of iterations or until there are no more mismatching points.
        diff = numel + 1;
        while( iter < numiters ) {
            diff_before = diff;
            diff = get_misclass_points(src,tgt,cmsk,numel,pts,tmp_pts);
            //printf("iter is %d, diff is %d, diff_before is %d\n",iter,diff,diff_before);
            if( diff_before == diff ) break;
    
            for( i = 0; i < diff; i++ ) {
                //pt = pts[i]; x = pt % m; y = (pt / m) % n; z = pt / m / n; // ind2sub for 3d, F-order
                pt = pts[i]; z = pt % nz; y = (pt / nz) % n; x = pt / n / nz; // ind2sub for 3d, C-order
                if( !get_nbhd_patch(patch,src,x,y,z,m,n,nz) ) return NULL; // raise error, meh
                ind = get_simpleLUTind_from_patch(patch);
                if( slow ) {
                    if( !get_nbhd_patch(new_patch,new_src,x,y,z,m,n,nz) ) return NULL; // raise error, meh
                    new_ind = get_simpleLUTind_from_patch(new_patch);
                    if( !simpleLUT[ind] && !simpleLUT[new_ind] ) new_src[pt] = !patch[13];
                } else {
                    if( !simpleLUT[ind] ) src[pt] = !patch[13];
                }
            } // for each misclassified point
    
            //if( slow ) memcpy( src, new_src, numel*sizeof(npy_bool) );
            if( slow ) memcpy( src, new_src, numel );
            iter++;
        } // descent loop
        
    } // for each gray level (only when useGray)

    // Optionally return by reference the type of all remaining nonsimple points after the warping.
    if( PyArray_SIZE(oNonSimple) > 0 ) {
        diff = get_misclass_points(src,tgt,msk,numel,pts,NULL);
        for( i = 0; i < diff; i++ ) {
            //pt = pts[i]; x = pt % m; y = (pt / m) % n; z = pt / m / n; // ind2sub for 3d, F-order
            pt = pts[i]; z = pt % nz; y = (pt / nz) % n; x = pt / n / nz; // ind2sub for 3d, C-order
            if( !get_nbhd_patch(patch,src,x,y,z,m,n,nz) ) return NULL; // raise error, meh
            nonSimple[pt] = simpleLUT[get_simpleLUTind_from_patch(patch)];
        } // for each misclassified point
    } // if returning type of remaining nonsimple points
  
    // clean up
    free(pts); 
    if( slow ) { free(new_src); free(tmp_pts); }
    if( useGray ) free(gry_msk);

    // return the remaining diff only, warped is returned by reference in src
    return Py_BuildValue("L", diff);
}


static PyObject *merge_supervoxels(PyObject *self, PyObject *args)
{
    PyArrayObject *data_cube, *merged, *consensus_label;
    int nlabels;
    if (!PyArg_ParseTuple(args, "O!O!O!i", &PyArray_Type, &consensus_label, &PyArray_Type, &data_cube, &PyArray_Type, 
            &merged, &nlabels)) {
        return NULL;
    }
    
    npy_uint32 *original; original = (npy_uint32 *) PyArray_DATA(data_cube);
    npy_int64 *to_merge; to_merge = (npy_int64 *) PyArray_DATA(merged);
    npy_uint32 *consensus; consensus = (npy_uint32 *) PyArray_DATA(consensus_label);
   
    npy_int m,n,z;
    npy_intp *dims;
    dims = PyArray_DIMS(data_cube); m = dims[0]; n = dims[1]; z = dims[2];
 
    npy_intp data_size, merged_size;
    data_size = PyArray_SIZE(data_cube);
    merged_size = PyArray_SIZE(merged);
    
    npy_intp i, j; int a = 5;
    //npy_intp a, b, c, rem;
    for (i = 0; i < data_size; i++) {       //iterate through data_cube
        for (j = 0; j < merged_size; j++) { //iterate through merged
            if (original[i] == to_merge[j]) {
                // for F-order:
                //a = i % m; rem = i / m; b = rem % n; c = rem / n;
                //consensus[(a * n * z) + (b * z) + c] = nlabels;
                
                // for C-order:
                consensus[i] = nlabels;
            }
        }
    }
    
    return Py_BuildValue("L", 0);
}


static PyObject *type_components(PyObject *self, PyObject *args)
{
    PyArrayObject *labels, *voxel_type, *supervoxel_type, *voxel_out_type;
    npy_uint32 *lbls, num_types;
    npy_uint8 *svclass, *vclass, *voclass;
    
    npy_int m,n,z, i,j,k, imax;
    npy_intp *dims, size, nsupervoxels, ind;
    npy_uint64 **counts, cmax;

    // Using the same method as for affinities, mostly for convenience in not have to re-write much.    
    // if a voxel has already been previously labeled, it will not be reassigned.
    // curlab is also taken as input as to the label to start with (usually one plus the max in the input labels)
    if (!PyArg_ParseTuple(args, "O!O!O!O!I", &PyArray_Type, &labels, &PyArray_Type, &voxel_type, 
            &PyArray_Type, &supervoxel_type, &PyArray_Type, &voxel_out_type, &num_types)) 
        return NULL;
    
    /* Get the dimensions of the labels and the c pointer to the labels and components */
    dims = PyArray_DIMS(labels); m = dims[0]; n = dims[1]; z = dims[2]; size = m*n*z;
    nsupervoxels = PyArray_SIZE(supervoxel_type);
    lbls = (npy_uint32 *) PyArray_DATA(labels);
    vclass = (npy_uint8 *) PyArray_DATA(voxel_type);
    svclass = (npy_uint8 *) PyArray_DATA(supervoxel_type);
    voclass = (npy_uint8 *) PyArray_DATA(voxel_out_type);
    
    /* Allocate counts for each type. */
    counts = (npy_uint64 **) malloc((size_t) (num_types*sizeof(npy_uint64*)));
    if( counts == NULL ) {
        printf("In type_components allocation of memory for counts failed."); exit(0);
    }
    for( i=0; i<num_types; i++ ) {
        counts[i] = (npy_uint64 *) calloc(nsupervoxels, (size_t) sizeof(npy_uint64));
        if( counts[i] == NULL ) {
            printf("In type_components allocation of memory for counts failed."); exit(0);
        }
    }

    for( ind=0; ind<size; ind++ ) {
        // if voxel is labeled and identifying type is less than specified numclasses, increment type count
        if( lbls[ind] && vclass[ind] < num_types ) counts[vclass[ind]][lbls[ind]-1]++;
    } // for each voxel

    // get the max votes for each supervoxel and assign the type for each supervoxel
    for( ind=0; ind<nsupervoxels; ind++ ) {
        for( i=0, cmax=0, imax=0; i<num_types; i++ ) {
            if( counts[i][ind] >= cmax ) {
                cmax = counts[i][ind]; imax = i;
            }
        }
        svclass[ind] = imax;
    }

    // re-assign the type of each supervoxels at the voxel level
    for( ind=0; ind<size; ind++ ) {
        if( lbls[ind] ) voclass[ind] = svclass[lbls[ind]-1];
    } // for each voxel
    
    // clean up
    for( i=0; i<num_types; i++ ) free(counts[i]);
    free(counts);

    return Py_BuildValue("L", 0);
}


/* #### Helper functions for EM data extensions #################################### */

npy_intp get_misclass_points(const npy_bool *src, const npy_bool *tgt, const npy_bool *msk, npy_intp numel, 
        npy_intp *pts, npy_intp *tmp_pts) {
    npy_intp i, diff = 0, hdiff, *cpts = (tmp_pts == NULL ? pts : tmp_pts);
  
    for( i = 0; i < numel; i++ ) {
        // mask and xor(src,tgt)
        if( msk[i] && ((src[i] && !tgt[i]) || (!src[i] && tgt[i])) ) cpts[diff++] = i;
    }
  
    if( tmp_pts != NULL ) {
        // re-order by interleaving points from beginning and end, helps give better warps in slow (perimeter) mode
        hdiff = diff/2;
        for( i = 0; i < hdiff; i++ ) {
            pts[2*i] = tmp_pts[i]; pts[2*i+1] = tmp_pts[diff - i - 1];
        }
        if( diff > 0 ) pts[diff-1] = tmp_pts[hdiff]; // last point if odd diff
    }
    
    return diff;
}

int get_nbhd_patch(npy_bool *patch, const npy_bool *src, npy_int x, npy_int y, npy_int z, npy_int m, npy_int n, 
        npy_int nz) {
    npy_intp ind;
    int xp,yp,zp;

    // this should not happen (mask should be zero around edges)
    //if( x < 1 || x > m-1 || y < 1 || y > n-1 || z < 1 || z > nz-1 ) {
    //    printf("get_nbhd_patch: %d,%d,%d out of bounds %dx%dx%d\n",x,y,z,m,n,nz);
    //    return 0;
    //}
  
    for( zp = -1; zp < 2; zp++ ) {
        for( yp = -1; yp < 2; yp++ ) {
            for( xp = -1; xp < 2; xp++ ) {
                //ind = (z+(npy_intp)zp)*m*n + (y+(npy_intp)yp)*m + (x+(npy_intp)xp); // sub2ind for 3d, F-order
                ind = (npy_intp)(x+xp)*n*nz + (npy_intp)(y+yp)*nz + (npy_intp)(z+zp);  // sub2ind for 3d, C-order
        
                patch[(zp+1)*9 + (yp+1)*3 + (xp+1)] = src[ind];     // patch lookup was created as F-order in matlab
            }
        }
    }
    return 1;
}

npy_uint32 get_simpleLUTind_from_patch(npy_bool *patch) {
    npy_uint32 ind = 0, i;
    // LUT is stored in F-order for binarized 3x3x3 patch
    for( i = 0; i < 27; i++ ) if( patch[i] > 0 ) ind |= (1 << i);
    return ind;
}


