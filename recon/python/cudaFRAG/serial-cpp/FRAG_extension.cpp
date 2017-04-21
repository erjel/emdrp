/****************************************************
 * Author - Rutuja
 * Date - 2/27/2016
 * Extension to convert the python data structures 
 * to cpp data structures. 
******************************************************/
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
    if (!PyArg_ParseTuple(args, "OiiiOiOiiOO", &input_watershed, &n_supervoxels, &connectivity, &size_of_edges, &input_edges, &verbose, &input_steps, &adjacencyMatrix, &label_jump, &input_count, &input_edge_test)) 
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
    unsigned max_int = std::pow(2,32);
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
        // reinitialize the hybrid adjacency matrix to zero for next iteration
        // this step takes almost more than 50% of the executon time of the algorithm
        // Need to find faster way to initialize arrays with zeros on the cpu
        memset(edge_test, 0 , (n_supervoxels*label_jump)*sizeof(npy_uint8));
    }
  
    //post processing 
    std::sort(list_of_edges.begin(), list_of_edges.end()); 
    auto last = std::unique(list_of_edges.begin(), list_of_edges.end());
    list_of_edges.erase(last, list_of_edges.end()); 
    count[0] = list_of_edges.size();
    std::cout << "total edges generated for this volume: " <<  count[0] << std::endl; 
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
    npy_uint32 n_voxels, n_borders;
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
    if(verbose) std::cout << "edges" << edges[18313*2 +1] << std::endl;
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
                if(edge_val != label && edge_val != 0){
                    it = tmp_edges.begin();
                    while(it != tmp_edges.end()){
                        if(watershed[vox + steps_border[step]] == *it){
                            edge_val = *it;
                        //std::cout << "label: " <<  label << "edge: " << edge_val << std::endl;
                            dilation_index = vox + steps_border[step]; 
                            // get the index rank of the edge for which boundary is being calculated
                            ind  = std::find(setofB.begin() + store_index, setofB.end(), edge_val);
                            if(ind != setofB.end()){
                                start_index = (ind - setofB.begin())/2;
                            }
                            // calculate the indices of the edge and check if they match to the edges in the structure
                            if(borders[start_index*n_borders_dim[1] + 0] == label){
                                if(borders[start_index*n_borders_dim[1] + 1] == edge_val){

                                     assert(true);

                                }else{

                                     assert(false);
                                } 

                            }else{

                                assert(false);
                            }  
                        
                            //get the indices  that form a border with this edge
                            get_dilation(dilation1, dilation2, steps_edges, n_steps_edges, dilation_index, 
                                         vox, borders, start_index, n_borders_dim[1]);
                            //get_comparison(dilation_1x, steps_edges, n_steps_edges, 
                              //         vox, borders, start_index, n_borders_dim[1]);
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
                     if(borders[start_index*n_borders_dim[1] + 0] == label){
                         if(borders[start_index*n_borders_dim[1] + 1] == edge_val){
                             
                             assert(true);

                         }else{

                             assert(false);
                         }

                     }else{

                        assert(false);
                     }

                     get_dilation(dilation1, dilation2, steps_edges, n_steps_edges, dilation_index,
                                  vox, borders, start_index, n_borders_dim[1]);
                     //get_comparison(dilation_1x, steps_edges, n_steps_edges,
                       //             vox, borders, start_index, n_borders_dim[1]);        
                 } 
             }
          
         }
    }

    //postprocessing
    for(int edge_size = 0; edge_size < count[0] ; edge_size++){ 
         
         unsigned int start = edge_size*n_borders_dim[1] + 3;
         unsigned int end = edge_size*n_borders_dim[1] + borders[edge_size*n_borders_dim[1] + 2];
         std::vector<npy_uint32> indices(borders + start, borders + end);
         std::sort(indices.begin() , indices.end());
         std::copy(indices.begin(), indices.end(), borders + edge_size*n_borders_dim[1] + 3);
   
    }
  
    return Py_BuildValue("i",1);

}


void get_dilation(unsigned int* dila_1, unsigned int* dila_2, 
                  npy_intp* steps, npy_int n_steps, unsigned int dila_index1, 
                  unsigned int dila_index2, npy_uint32* boundary, 
                  unsigned int start_index, npy_uint32 border_dim){

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
               assert(boundary[start_index*border_dim + 2] < border_dim); 
               unsigned int cnt = boundary[start_index*border_dim + 2];
               for(unsigned int f = 3;f < cnt ;f++){
                   if(dila_1[h] ==  boundary[start_index*border_dim + f]){
                      do_add = false;
                      break;
                   }                  
               }
               if(do_add){
                   boundary[start_index*border_dim + cnt] = dila_1[h];
                   boundary[start_index*border_dim + 2] += 1;
               }
            }
        }
    }    
}

void get_comparison(unsigned int* dilation, npy_intp* steps, npy_int n_steps, unsigned int ind, 
                    npy_uint32* boundary, unsigned int start_index, npy_uint32 border_dim){

    npy_int indices[n_steps];
    
    for(npy_int j=0;j < n_steps; j++){
        indices[j] = steps[j] + ind;
    }
    
    std::vector<unsigned int> setofA(indices, indices + n_steps); 

    // calculate the intersecton of the dilated indices and add them to the structure
    /* for (npy_int i = 0; i < n_steps; ++i) {
        if (setofA.find(dilation[i]) != setofA.end()) {
         
           assert(boundary[start_index*border_dim + 2] < border_dim); 
           unsigned int cnt = boundary[start_index*border_dim + 2];
           boundary[start_index*border_dim + cnt] = dilation[i];
           boundary[start_index*border_dim + 2] += 1;
        }
    }*/
    
}