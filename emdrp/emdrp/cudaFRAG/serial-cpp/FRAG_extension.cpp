/*************************************************************
 * Author - Rutuja
 * Date - 2/27/2016
 * Function - Extension to convert the python data structures 
 * to cpp data structures and crate RAG and calculate borders
*************************************************************/
//C extension includes
#include "Python.h"
#define NPY_NO_DEPRECATED_API NPY_1_10_API_VERSION
#include "arrayobject.h"

//system includes
#include <iostream>
#include <time.h>
#include <tuple>
#include <set>
#include <vector>
#include <algorithm>
#include <assert.h>
#include <unordered_set>

#include "timer.h"
#include "FRAG_extension.h"
#define DEBUG_NEW new(__FILE__, __LINE__)

// Methods Table
static PyMethodDef _frag_ExtensionMethods[] = {
    // EM data extensions
    {"build_frag", build_frag, METH_VARARGS},
    {"build_frag_borders", build_frag_borders, METH_VARARGS},
    {"build_frag_borders_nearest_neigh", build_frag_borders_nearest_neigh, METH_VARARGS},
    {"build_frag_new", build_frag_new, METH_VARARGS},
    {NULL, NULL}     /* Sentinel - marks the end of this structure */
};

/* ==== Initialize the C_test functions ====================== */
// Module name must be _pyCext in compile and linked

// https://docs.python.org/3.3/howto/cporting.html
// http://python3porting.com/cextensions.html

#if PY_MAJOR_VERSION >= 3
static struct PyModuleDef moduledef = {
        PyModuleDef_HEAD_INIT,
        "_FRAG_extension",           /* m_name */
        NULL,                /* m_doc */
        -1,                  /* m_size */
        _frag_ExtensionMethods,      /* m_methods */
        NULL,                /* m_reload */
        NULL,
        NULL,
        NULL
};
  
PyMODINIT_FUNC
PyInit__FRAG_extension(void)
#else
void init_FRAG_extension()
#endif
{
#if PY_MAJOR_VERSION >= 3
    PyObject *module = PyModule_Create(&moduledef);
#else
    (void) Py_InitModule("_FRAG_extension", _frag_ExtensionMethods);
    //PyObject *module = Py_InitModule("myextension", myextension_methods);
#endif

    import_array();  // Must be present for NumPy.  Called first after above line.

#if PY_MAJOR_VERSION >= 3
    return module;
#endif
}

// Method to create RAG 
static PyObject *build_frag(PyObject *self, PyObject *args){

    PyArrayObject *input_watershed;
    PyArrayObject *input_edges;
    PyArrayObject *input_steps;
    PyArrayObject *input_count;
    PyArrayObject *input_edge_test;
    npy_int n_steps, connectivity;
    npy_uint32 n_voxels, n_supervoxels, size_of_edges, label_jump;
    npy_intp* dims;
    npy_intp* n_voxels_dim;
    int verbose, adjacencyMatrix;
 
   
    // parse arguments
    if (!PyArg_ParseTuple(args, "OiiiOiOiiOO", &input_watershed, &n_supervoxels, &connectivity, &size_of_edges, 
                          &input_edges, &verbose, &input_steps, &adjacencyMatrix, &label_jump, &input_count, &input_edge_test)) 
       return NULL;

    // get arguments in PythonArrayObject to access data through C data structures
    // get watershed unraveled array
    unsigned int *watershed;
    watershed = (unsigned int*)PyArray_DATA(input_watershed);
    dims = PyArray_DIMS(input_watershed);
    n_voxels_dim = dims;
    n_voxels = n_voxels_dim[0]*n_voxels_dim[1]*n_voxels_dim[2];
    if(verbose) std::cout << "number of watershed pixels" << n_voxels << " " << n_voxels_dim[0] << " " << n_voxels_dim[1] << " " <<  n_voxels_dim[2] << std::endl;

    // necessary to typecast "steps" with npy_intp* ,otherwise we get wrong results
    npy_intp *steps;
    steps = (npy_intp*)PyArray_DATA(input_steps);
    dims = PyArray_DIMS(input_steps);
    n_steps = dims[0];
    if (verbose) std::cout << "number of steps " << n_steps << " " << steps[1] << " " << steps[2] << " " << steps[25] << std::endl;
     
    std::vector<std::tuple<int,int>> list_of_edges;
    
    // get the hybrid_adjacency matrix data structure and the edges and count for edges
    npy_uint8* edge_test;
    edge_test = (npy_uint8*)PyArray_DATA(input_edge_test); 
    npy_int32 *edges = (npy_int32*)PyArray_DATA(input_edges);
    npy_int32 *count = (npy_int32*)PyArray_DATA(input_count);

    //check if there is an overflow for space allocated to hybrid adjacency matrix
    unsigned int max_int = 4294967295; // the value 2^32-1 to check for overflow
    unsigned int size = max_int/n_supervoxels;
    assert(label_jump < size);
    
    GpuTimer timer1;
    timer1.Start();
    npy_uint32 label;
    npy_uint32 index;
    npy_uint32 edge_value;
    //creation of rag
    for(unsigned int start_label = 1; start_label < n_supervoxels; start_label += label_jump){
       
        for(unsigned int vox = 0; vox < n_voxels; vox++){
            
            label = watershed[vox];
            
            if(label !=0 && label < (start_label + label_jump) && label >= start_label){ 
            
                for(int step = 0;step < n_steps;step++){
                    
                    edge_value = watershed[vox + steps[step]];
                    
                    if(edge_value > label){
                    
                        if(adjacencyMatrix){
                            
                            if(label <= label_jump){
                            
                                index = (label-1)*n_supervoxels + edge_value - 1;
                            
                            } else{
                            
                                index = (label - start_label)*n_supervoxels + edge_value - 1;
                            }  
                          
                            if(edge_test[index] == 0){
                            
                                list_of_edges.push_back(std::tuple<int,int>(label,watershed[vox + steps[step]]));
                                edge_test[index] = 1;
                            }  
                          
                        }else{
                      
                                list_of_edges.push_back(std::tuple<int,int>(label,watershed[vox + steps[step]]));
                        }
                    }      
                }
            }
        }
        if(adjacencyMatrix){
            // reinitialize the hybrid adjacency matrix to zero for next iteration
            // this step takes almost more than 50% of the executon time of the algorithm
            // Need to find faster way to initialize arrays with zeros on the cpu
            memset(edge_test, 0 , (n_supervoxels*label_jump)*sizeof(npy_uint8));
        }
    }
  
    //post processing 
    std::sort(list_of_edges.begin(), list_of_edges.end()); 
    auto last = std::unique(list_of_edges.begin(), list_of_edges.end());
    list_of_edges.erase(last, list_of_edges.end()); 
    count[0] = list_of_edges.size();
    std::cout << "total edges generated for this volume: " <<  count[0] << std::endl; 
    std::cout << "size of edges " << size_of_edges << std::endl;
    timer1.Stop();
    float total_time = timer1.Elapsed()/1000;
    std::cout << "total rag_creation time: " << total_time << std::endl;

    // check if the size of edges is enough to accomodate the edges generated
    assert(size_of_edges > count[0]);

    // copy the generated edges into the appropriate data structure
    std::vector<std::tuple<int,int>>::iterator i = list_of_edges.begin();
    unsigned int cnt = 0;
    for(i = list_of_edges.begin(); i != list_of_edges.end();i++){

            edges[cnt* 2 + 0] = std::get<0>(*i);
            edges[cnt* 2 + 1] = std::get<1>(*i);
            cnt++;
    }

    return Py_BuildValue("i",1);
    
}

// a RAG graph element, using the "array-of-lists" method for storing the RAG.
typedef struct {
        npy_uint32 value;
        std::vector<npy_uint64> *border_voxels;
        npy_intp last_border;
    } RAG;

// new Method to create RAG.
// method is a bit faster than adjacency matrix because the RAGs are always very sparse.
static PyObject *build_frag_new(PyObject *self, PyObject *args) {

    PyArrayObject *input_watershed, *input_steps;
    npy_uint32 *watershed, label, edge_value, cvalue;
    npy_intp *shp, n_voxels, n_steps, edge_count=0;
    npy_int32 *steps;
    npy_uint32 n_supervoxels, nalloc_rag, nalloc_borders; 
    int min_step, max_step;
    std::vector<RAG> *cedges;
    std::vector<npy_uint64> *cborder;
    RAG crag;
   
    // parse arguments
    if (!PyArg_ParseTuple(args, "O!IO!iiII", &PyArray_Type, &input_watershed, &n_supervoxels, 
                          &PyArray_Type, &input_steps, &min_step, &max_step, &nalloc_rag, &nalloc_borders)) 
       return NULL;

    // get arguments in PythonArrayObject to access data through C data structures
    // supervoxels or "watershed" label input
    shp = PyArray_DIMS(input_watershed);
    n_voxels = PyArray_SIZE(input_watershed);
    watershed = (npy_uint32 *) PyArray_DATA(input_watershed);

    // integers specifying where to look relative to current voxel    
    n_steps = PyArray_SIZE(input_steps);
    steps = (npy_int32 *) PyArray_DATA(input_steps);

    // allocate data structure for storing edges
    std::vector<RAG> **sparse_edges = new std::vector<RAG>* [n_supervoxels];
    for( npy_uint32 i=0; i < n_supervoxels; i++ ) {
        sparse_edges[i] = new std::vector<RAG>; sparse_edges[i]->reserve(nalloc_rag);
    }
    
    // creation of rag
    std::vector<RAG>::iterator it;
    std::vector<npy_uint64>::iterator jt;
    min_step = (min_step < 0 ? -min_step : 0); max_step = (max_step > 0 ? max_step : 0);
    for( npy_int64 vox = min_step, cvox; vox < n_voxels - max_step; vox++ ) {
        label = watershed[vox];
        if( label != 0 ) { 
            // iterate "steps" which is a list of relative indices (C-order) where to search for neighbors
            for(int step = 0; step < n_steps; step++) {
                cvox = vox + steps[step]; edge_value = watershed[cvox];
                if( edge_value != 0 && edge_value != label ) { // do not add any self-directed edges
                        
                    // only store "triangular" matrix so edges are not duplicated (RAG is not directed)
                    if( edge_value < label ) {
                        cedges = sparse_edges[label-1]; cvalue = edge_value;
                    } else {
                        cedges = sparse_edges[edge_value-1]; cvalue = label;
                    }

                    // search for the edge
                    // NOTE: initially found that keeping the edge list in descending order is significantly faster.
                    //     that is  it->value <= cvalue  instead of  it->value >= cvalue
                    //   likely this is a combination of C-order and the labeling order from the watershed.
                    for( it = cedges->begin(); it != cedges->end(); it++ ) 
                        if( it->value <= cvalue ) break;
                        
                    // store the edge if not already there
                    if( it == cedges->end() || it->value != cvalue ) {
                        //cborder = NULL;
                        //if( get_borders ) { 
                            cborder = new std::vector<npy_uint64>; 
                            cborder->reserve(nalloc_borders);
                            crag.last_border = 0;
                        //}
                        crag.value = cvalue; crag.border_voxels = cborder;
                        it = cedges->insert(it, crag); edge_count++;
                    } else {
                        cborder = it->border_voxels;
                    }
                        
                    // store the border voxels
                    //if( get_borders ) {
                        // method without last border for reference.
                        //for( jt = cborder->begin(); jt != cborder->end(); jt++ ) 
                        //    if( *jt <= cvox ) break;
                        //if( jt == cborder->end() || *jt != cvox ) 
                        //    jt = cborder->insert(jt, cvox);
                        //// NOTE: this is optimized assuming that cvox > vox
                        //for( /*jt = cborder->begin()*/; jt != cborder->end(); jt++ ) 
                        //    if( *jt <= vox ) break;
                        //if( jt == cborder->end() || *jt != vox ) 
                        //    cborder->insert(jt, vox);

                        // NOTE: this is optimized assuming both that vox is always incrementating
                        //   and that cvox is always positive. This means that for an increasing list the current
                        //   voxel always has to be past the location the last voxel was inserted in the border list.
                        for( jt = cborder->begin() + it->last_border; jt != cborder->end(); jt++ ) 
                            if( *jt >= vox ) break;
                        if( jt == cborder->end() || *jt != vox ) 
                            jt = cborder->insert(jt, vox);
                        it->last_border = jt - cborder->begin();
                        // NOTE: this is optimized assuming that cvox > vox
                        for( /*jt = cborder->begin()*/; jt != cborder->end(); jt++ ) 
                            if( *jt >= cvox ) break;
                        if( jt == cborder->end() || *jt != cvox ) 
                            cborder->insert(jt, cvox);
                    //}
                }
            }
        }
    }
    //std::cout << "edges " << edge_count << std::endl;

    // create a list of edges to return in numpy array format
    npy_intp eshp[2]; eshp[0] = edge_count; eshp[1] = 2;
    PyArrayObject *list_of_edges = (PyArrayObject *) PyArray_Empty(2, eshp, PyArray_DescrFromType(NPY_UINT32), 0);
    npy_uint32 *edges = (npy_uint32 *) PyArray_DATA(list_of_edges);
    npy_intp cnt=0, bcnt;
    npy_intp bshp[1];
    //PyObject* border_list = get_borders ? PyList_New(edge_count) : NULL;
    PyObject* border_list = PyList_New(edge_count);
    PyArrayObject *list_of_borders;
    npy_uint64 *borders;
    
    // copy the generated edges into the numpy array, free allocated memory
    for( npy_uint32 i=0; i < n_supervoxels; i++ ) {
        std::vector<RAG> *cedges = sparse_edges[i];
        for( std::vector<RAG>::iterator it = cedges->begin(); it != cedges->end(); it++, cnt++ ) {
            // NOTE: order that the edges are stored "upper or lower" triagular above, affects order here.
            //edges[2*cnt] = i+1; edges[2*cnt+1] = it->value; 
            edges[2*cnt] = it->value; edges[2*cnt+1] = i+1; 
            //if( get_borders ) {
                cborder = it->border_voxels; bshp[0] = cborder->size();
                list_of_borders = (PyArrayObject *) PyArray_Empty(1, bshp, PyArray_DescrFromType(NPY_UINT64), 0);
                borders = (npy_uint64 *) PyArray_DATA(list_of_borders); bcnt = 0;
                for( std::vector<npy_uint64>::iterator jt = cborder->begin(); jt != cborder->end(); jt++, bcnt++ )
                    borders[bcnt] = *jt;
                delete cborder;
                PyList_SetItem(border_list, cnt, (PyObject*) list_of_borders);
            //}
        }
        delete sparse_edges[i];
    }
    delete sparse_edges;
    //assert( cnt == edge_count );

    //return (PyObject *) list_of_edges; // to return single object
    return Py_BuildValue("OO", (PyObject *) list_of_edges, border_list);
}


// this is the border get features using Pauls dilation method. Slower than python on 
// gpu. 
static PyObject *build_frag_borders(PyObject *self, PyObject *args){

    PyArrayObject *input_watershed;
    PyArrayObject *input_edges;
    PyArrayObject *input_steps;
    PyArrayObject *input_count;
    PyArrayObject *input_borders;
    PyArrayObject *input_steps_border;
    npy_uint32 n_supervoxels;
    int verbose;
    npy_intp* dims;
    npy_intp *n_voxels_dim;
    npy_intp *n_borders_dim;
    npy_uint32 n_voxels;
    npy_uint64 n_borders;
    npy_int n_steps_edges,n_steps_border;

    // parse arguments
    if (!PyArg_ParseTuple(args,"OiOOOiOO", &input_watershed, &n_supervoxels, &input_edges, &input_borders, &input_count, &verbose, &input_steps, &input_steps_border))
       return NULL;

    // get the watershed voxels
    unsigned int *watershed = (unsigned int*)PyArray_DATA(input_watershed);
    dims = PyArray_DIMS(input_watershed);
    n_voxels_dim = dims;
    n_voxels = n_voxels_dim[0]*n_voxels_dim[1]*n_voxels_dim[2];
    if(verbose) std::cout << "number of watershed pixels" << n_voxels << " " << n_voxels_dim[0] << " " << n_voxels_dim[1] << " " <<  n_voxels_dim[2] << std::endl;

    // necessary to typecast "steps" with npy_intp* ,otherwise we get wrong results
    // get steps for 1X dilation
    npy_intp *steps_edges = (npy_intp*)PyArray_DATA(input_steps);
    dims = PyArray_DIMS(input_steps);
    n_steps_edges = dims[0];
    if (verbose) std::cout << "number of steps " << n_steps_edges << " " << steps_edges[1] << " " << steps_edges[2] << " " << steps_edges[25] << std::endl;

    //get steps for 2X dilation 
    npy_intp *steps_border = (npy_intp*)PyArray_DATA(input_steps_border);
    dims = PyArray_DIMS(input_steps_border);
    n_steps_border = dims[0];
    if (verbose) std::cout << "number of steps " << n_steps_border << " " << steps_border[1] << " " << steps_border[2] << " " << steps_border[25] << std::endl;

    //get the edges and borders 
    npy_uint32 *edges = (npy_uint32*)PyArray_DATA(input_edges);
    npy_int32 *count = (npy_int32*)PyArray_DATA(input_count);
  
    npy_uint32 *borders = (npy_uint32*)PyArray_DATA(input_borders);
    dims = PyArray_DIMS(input_borders);
    n_borders_dim = dims;
    n_borders = n_borders_dim[0]*n_borders_dim[1];
    if(verbose) std::cout << "size of borders: " << n_borders << std::endl;
 
    std::vector<unsigned int>tmp_edges;
    npy_uint32 label;
    npy_uint32 prev_label = watershed[0];
    npy_uint32 edge_val;
    unsigned int* dilation1 = (unsigned int*)malloc(n_steps_edges*sizeof(unsigned int)); 
    unsigned int* dilation2 = (unsigned int*)malloc(n_steps_edges*sizeof(unsigned int));
    unsigned int iter= 0;
    unsigned int dilation_index; 
    std::vector<unsigned int>::iterator it; 
    unsigned int store_index = 0;
    unsigned int start_index = 0;
    npy_uint64 start = 0;
    std::vector<npy_int32> setofB(edges, edges + count[0]*2);
    std::vector<npy_int32>::iterator ind;
    
    for(unsigned int vox = 0;vox < n_voxels; vox++){
     
        label = watershed[vox];
        
        if(label != 0 && prev_label != label){
        
            tmp_edges.clear();            
            
            for(unsigned int iter_edges = 0;iter_edges < (2*count[0]); iter_edges += 2){
                
                if(label == edges[iter_edges]){
                
                    store_index = iter_edges;
                    
                    while(edges[iter_edges] == label){   
                     
                        tmp_edges.push_back(edges[iter_edges+1]);
                    
                        if((iter_edges+2) < (2*count[0])){
                        
                            iter_edges += 2;
                           
                        }else{
                       
                           break;
                        }
                   }
                }      
                
                if(!tmp_edges.empty()) break;
            
            }
            
            prev_label = label;      
        }
        
        // finding the borders in the 2X dilation region
        if(!tmp_edges.empty() && label != 0){
        
           for(int step = 0; step < n_steps_border; step++){
       
               edge_val = watershed[vox + steps_border[step]];
               
               if(edge_val > label && edge_val != 0){
               
                   it = tmp_edges.begin();
             
                   while(it != tmp_edges.end()){
             
                       if(watershed[vox + steps_border[step]] == *it){
                       
                           edge_val = *it;
                           dilation_index = vox + steps_border[step]; 
                           // get the index rank of the edge for which boundary is being calculated
                           ind  = std::find(setofB.begin() + store_index, setofB.end(), edge_val);
                           if(ind != setofB.end()){
                               
                               start_index = (ind - setofB.begin())/2;
                           }
                           // calculate the indices of the edge and check if they match to the edges in the structure
                           start = start_index*n_borders_dim[1];   
                            
                           if(borders[start] == label){
                               
                               if(borders[start + 1] == edge_val){

                                     assert(true);

                               }else{

                                     assert(false);
                               } 

                           }else{

                                assert(false);
                           }  
                        
                           //get the indices  that form a border with this edge
                           get_dilation(dilation1, dilation2, steps_edges, n_steps_edges, dilation_index, 
                                         vox, borders, start, n_borders_dim[1]);
                           it++;
                        
                       }else{
                          
                           it++;
                       } 
                   }
               }
           } 

           for(int step = 0;step < n_steps_edges; step++){
              
           edge_val = watershed[vox + steps_edges[step]];
             
               if(edge_val > label && edge_val !=0){
                   
                   dilation_index = vox + steps_edges[step];
                   ind  = std::find(setofB.begin() + store_index, setofB.end(), edge_val);
                   if(ind != setofB.end()){
                   
                       start_index = (ind - setofB.begin())/2;
                   
                   }
                   start = start_index*n_borders_dim[1];
                   
                   if(borders[start] == label){
                      
                       if(borders[start + 1] == edge_val){
                             
                             assert(true);

                       }else{

                             assert(false);
                       }

                   }else{

                        assert(false);
                   }

                   get_dilation(dilation1, dilation2, steps_edges, n_steps_edges, dilation_index,
                                  vox, borders, start, n_borders_dim[1]);
                } 
            }
        }
    }

    //postprocessing
    npy_uint64 edge_index = 0;
    npy_uint64 begin = 0;
    npy_uint64 end = 0;
    std::cout << "post_processing_started" << std::endl; 
    
    for(unsigned int edge_size = 0; edge_size < count[0] ; edge_size++){ 
     
        edge_index = edge_size * n_borders_dim[1];
         
        if(borders[edge_index + 2] >  20000)
              std::cout << borders[edge_index + 2] << std::endl; 
          
        begin = edge_index + 3;
        end = edge_index + borders[edge_index + 2];
        std::vector<npy_uint32> indices(borders + begin, borders + end);
        std::sort(indices.begin() , indices.end());
        std::copy(indices.begin(), indices.end(), borders + edge_index + 3);
   
    }
 
    free(dilation1);
    free(dilation2); 
    return Py_BuildValue("i",1);

}


void get_dilation(unsigned int* dila_1, unsigned int* dila_2, 
                  npy_intp* steps, npy_int n_steps, unsigned int dila_index1, 
                  unsigned int dila_index2, npy_uint32* boundary, 
                  npy_uint64 start_index, npy_uint32 border_dim){

    unsigned int cnt;
    //npy_int indices[n_steps];
    
    for(npy_int k = 0;k < n_steps; k++){
         dila_1[k] = dila_index1 + steps[k];
         dila_2[k] = dila_index2 + steps[k];
    }  
    
    bool do_add = true;
    // using simple hand rolled O(N^2) search is faster than unordered set or vector search
    for(npy_int h = 0; h < n_steps;h++){
    
        for(npy_int j = 0;j < n_steps; j++){
         
            if(dila_1[h] == dila_2[j]){
         
               do_add = true;
               //std::cout << boundary[start_index] << "-" << boundary[start_index + 1] 
               //<< "-" << boundary[start_index + 2] << std::endl;
               // chaeckimg to see id inex already present
               assert(boundary[start_index + 2] < border_dim); 
               cnt = boundary[start_index + 2];
               for(unsigned int f = 3;f < cnt ;f++){
                   if(dila_1[h] ==  boundary[start_index + f]){
                      do_add = false;
                      break;
                   }                  
               }
               if(do_add){
                   boundary[start_index  + cnt] = dila_1[h];
                   boundary[start_index + 2] += 1;
               }
            }
        }
    }    
}

// get border features using the nearest neighour similar to GALA.
static PyObject *build_frag_borders_nearest_neigh(PyObject *self, PyObject *args){

    PyArrayObject *input_watershed;
    PyArrayObject *input_borders;
    PyArrayObject *input_steps;
    PyArrayObject *input_count;
    npy_uint32 n_supervoxels;
    bool verbose;
    npy_intp* dims;
    npy_intp *n_voxels_dim;
    npy_intp *n_borders_dim;
    npy_uint32 n_voxels;
    npy_uint64 n_borders;
    npy_int n_steps;
   
//parse arguments
    if (!PyArg_ParseTuple(args,"OiOOiO", &input_watershed, &n_supervoxels, &input_borders, &input_count, &verbose, &input_steps))
        return NULL;
             
     // get the watershed voxels
    unsigned int *h_watershed = (unsigned int*)PyArray_DATA(input_watershed);
    dims = PyArray_DIMS(input_watershed);
    n_voxels_dim = dims;
    n_voxels = n_voxels_dim[0]*n_voxels_dim[1]*n_voxels_dim[2];
    if(verbose) std::cout << "number of watershed pixels" << n_voxels << " " << n_voxels_dim[0] << " " << n_voxels_dim[1] << " " <<  n_voxels_dim[2] << std::endl;

    // necessary to typecast "steps" with npy_intp* ,otherwise we get wrong results
    // get steps for checking neighborhood 
    npy_intp *h_steps_edges = (npy_intp*)PyArray_DATA(input_steps);
    dims = PyArray_DIMS(input_steps);
    n_steps = dims[0];
    if (verbose) std::cout << "number of steps " << n_steps << " " << h_steps_edges[1] << " " << h_steps_edges[2] << " " << h_steps_edges[25] << std::endl;

    //get the edges and borders 
    //npy_uint32 *h_edges = (npy_uint32*)PyArray_DATA(input_edges);
    npy_int32 *h_count = (npy_int32*)PyArray_DATA(input_count);

    // get the structure to store borders
    npy_uint32 *h_borders = (npy_uint32*)PyArray_DATA(input_borders);
    dims = PyArray_DIMS(input_borders);
    n_borders_dim = dims;
    n_borders = n_borders_dim[0]*n_borders_dim[1];
    if(verbose) std::cout << "size of borders: " << n_borders << "-" << n_borders_dim[1] << std::endl;

    // get the borders for the list of edges 
    
    npy_uint32 edge_value = 0;   
    npy_uint32 label = 0;

    GpuTimer timer1;
    GpuTimer timer2;
    float time_processing = 0;
    npy_uint32 border_index_1 = 0;
    npy_uint32 border_index_2 = 0;
    std::vector<std::tuple<npy_uint32,npy_uint32,npy_uint32>>borders;
    timer1.Start();
    for(unsigned int vox = 0; vox < n_voxels; vox++){
        
        label = h_watershed[vox]; 
        if(label!=0){
        
            // calculate the number of edges before the current edge in ascending order

            for(int step = 0;step < n_steps;step++){
                edge_value = h_watershed[vox + h_steps_edges[step]];
          
                if(edge_value > label){
                    // calculate the index of the current edge 
                    border_index_1 = vox  + h_steps_edges[step];
                    border_index_2 = vox;
                    borders.push_back(std::tuple<npy_uint32, npy_uint32, npy_uint32>(label,edge_value,border_index_1));
                    borders.push_back(std::tuple<npy_uint32, npy_uint32, npy_uint32>(label,edge_value,border_index_2));
                }
                    
                
            }
        }
    }
    timer1.Stop();
    time_processing = timer1.Elapsed()/1000;
    //postprocessing
    npy_uint64 edge_index = 0;
    npy_uint64 begin = 0;
    npy_uint64 end = 0;
    std::cout << "post_processing_started" << std::endl;
    timer2.Start();
    std::sort(borders.begin(), borders.end());
    auto last = std::unique(borders.begin(), borders.end());
    borders.erase(last, borders.end());
    std::vector<std::tuple<npy_uint32,npy_uint32,npy_uint32>>::iterator i = borders.begin();
    npy_uint32 cnt = 0;
    npy_uint32 cur_label = std::get<0>(borders.at(0));
    npy_uint32 cur_edge = std::get<1>(borders.at(0));

    // copy the borders in the respective python datastructure for validation or further use    
    for(i = borders.begin(); i != borders.end();i++){
    
        if(std::get<0>(*i) == cur_label){
        
            if(std::get<1>(*i) == cur_edge){
        
                h_borders[cnt*n_borders_dim[1] + h_borders[cnt*n_borders_dim[1]+2]] = std::get<2>(*i);  
                h_borders[cnt*n_borders_dim[1]+2] += 1;
        
            }else{
             
                cnt++;
                cur_edge = std::get<1>(*i);
                h_borders[cnt*n_borders_dim[1] + h_borders[cnt*n_borders_dim[1]+2]] = std::get<2>(*i);
                h_borders[cnt*n_borders_dim[1]+2] += 1;
            }
        }else{
            
            cnt++;
            cur_label = std::get<0>(*i);
            cur_edge = std::get<1>(*i);
            h_borders[cnt*n_borders_dim[1] + h_borders[cnt*n_borders_dim[1]+2]] = std::get<2>(*i);
            h_borders[cnt*n_borders_dim[1]+2] += 1;
        }        
    }

    timer2.Stop();
    float post_processing = timer2.Elapsed()/1000;
    std::cout << "the processing time is" << time_processing << std::endl;
    std::cout << "the post processing time is" << post_processing << std::endl;
    return Py_BuildValue("i",1);
}
