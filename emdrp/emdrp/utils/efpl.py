# Python version of the knossos_efpl.m
import numpy as np

from typing import NamedTuple, Optional, List
import numpy.typing as npt

from numpy.ma import array, notmasked_edges

class Info(NamedTuple):
    """
    Low-level structure which holds skeleton edge information. Leftover from Matlab 
    implementation.

    Attributes:
        edges: List(ArrayLike[int]): Nodes which mark endpoint of each edge.
    """
    edges: List[npt.ArrayLike]


class Outputs(NamedTuple):
    """
    A emdrp output structure. Leftover from Matlab implementation.
    
    Attributes:
        nThings (int): A unique identifier
        omit_things_use (ArrayLike[bool]): Indexes False if "tree" with index is not included
            in analysis.
        nedges (ArrayLike[int]): Number of "edges" for each "tree".
        edge_length (list(ArrayLike[float])): Contains physical length of each edge in each tree.
        edges_use (list(ArrayLike[bool])): Marks edges which are included in the analysis.
        info (Info): Low level information of the involved nodes for each edge.
        path_length_use (ArrayLike(float)): Total length of all usable (defines be "edge_use") edges.
    """

    nThings: int
    omit_things_use: List[npt.ArrayLike]
    nedges: npt.ArrayLike
    edge_length: List[npt.ArrayLike]
    edges_use: List[npt.ArrayLike]
    info: Info
    path_length_use: npt.ArrayLike

    @classmethod
    def gen_from_nml(self, nml):
        """
        Shortcut to create Outputs instance from nml.

        Returns:
            o (Outputs): Outputs instance which match nml.
        """
        tree_infos = [Info(
                    edges =  np.array([[e.source, e.target] for e in t.edges])
                    ) for t in nml.trees]

        edge_lengths = []
        for t in nml.trees:
            tree_edge_lengths = []
            node_ids = [n.id for n in t.nodes]
            node_positions = np.array([n.position for n in t.nodes])
            for edge in t.edges:
                p1 = node_positions[node_ids.index(edge.source), :]
                p2 = node_positions[node_ids.index(edge.target), :]
                edge_length = np.linalg.norm((p1-p2) * nml.parameters.scale)
                tree_edge_lengths.append(edge_length)
            edge_lengths.append(tree_edge_lengths)

        self.nThings = len(nml.trees)
        self.omit_things_use =  np.zeros((len(nml.trees), 1), dtype=bool)
        self.nedges =  [len(t.edges) for t in nml.trees]
        self.edge_length  = edge_lengths
        self.edges_use =  [np.ones((len(t.edges),), dtype=bool) for t in nml.trees]
        self.info = tree_infos
        self.path_length_use =  [np.sum(edge_length) for edge_length in edge_lengths]

        return self


class Parameters(NamedTuple):
    """
    A emdrp parameter object.

    Defines the parameter for the error free path length calculation. The calculation
    depends on the error definition. A for-loop over the different implementations is
    controled by the `npasses_edge` attribute which defines the range for the loop.
    In loop (0): only splits are considered as errors. (1): only mergers. (2): split or
    merger errors, and (3) split and merger errors.

    Attributes:
        npasses_edges (int): Path length error definition.
        nalloc (Optional[int]): number of initialized element for edge stack.
        empty_label (Optional[int]): Label value which corresponds to background.
        count_half_error_edges (Optional[bool]): Controls the path length counting for edges with errors.
            (False) deletes path length completely. (True) splits the edge into halves
            and thus preserves the total path length.
        tol (Optional[float]): Tolerance threshold for assert sanity checks.
    """

    npasses_edges: int
    nalloc: Optional[int] = int(1e6)
    empty_label: Optional[np.uint32] = np.uint(2**32-1)
    count_half_error_edges: Optional[bool] = True
    tol: Optional[float] = 1e-5


def labelsWalkEdges(o,p,edge_split, label_merged, nodes_to_labels, rand_error_rate=None):
    """ Translated from MATLAB implementation:
    https://github.com/elhuhdron/emdrp/blob/00fefd48b6610df721fb6197835d824e23bb10af/recon/matlab/knossos/knossos_efpl.m#L978
    """
    # rand_error_rate is optional, but if specified contains all non-negative entries,
    #   then make a pass for each rand error rate entry.
    if rand_error_rate is None:
        rand_error_rate = np.array([-1])
    else:
        rand_error_rate = np.array(rand_error_rate)

    npasses = len(rand_error_rate)

    # if rand_error_rate is not specified or has negative entries then make four passes:
    #   (1) splits only (2) mergers only (3) split or merger errors (4) split and merger errors
    if np.any(np.array(rand_error_rate) < 0):
        npasses = p.npasses_edges
        rand_error_rate = -np.ones((npasses,))

    efpl = [np.zeros((p.nalloc,1)) for i in range(npasses)]
    efpl_cnt = np.zeros((npasses,), dtype=int)
    # keep track of where each thing starts on the efpl lists
    efpl_thing_ptr = np.zeros((o.nThings,npasses), dtype=int)

    # feature added after the paper, associated each edge with it's final efpl for it's "connected edges"
    efpl_edges = [[[] for n in range(o.nThings)] for pass_ in range(npasses) ]

    randcnt = 0
    rands = np.random.rand(p.nalloc);  # for rand_error_rate
    nalloc = 5000;   # local allocation max, just for node stacks
    # need to calculate split efpl and merger efpl separately
   
    if (nodes_to_labels is not None):
        # Requirement: Node ids are continous and start from 0 so that they can be used as indices: 
        for n in range(o.nThings):
            assert(np.all(np.arange(len(nodes_to_labels[n]), dtype=int) == np.unique([id for edge in o.info[n].edges for id in edge])))

    for pass_ in range(npasses):
        for n in range(o.nThings):
            if o.omit_things_use[n]:
                continue
            efpl_thing_ptr[n,pass_] = efpl_cnt[pass_]+1

            # feature added after the paper, associated each edge with it's final efpl for it's "connected edges".
            # only do this for error free edges (as error edges either don't count, or count half on two different componets).
            efpl_edges[pass_][n] = np.full((2,o.nedges[n]), np.nan)
            edges_comps = np.zeros((2,o.nedges[n]))
            ncomps = 0

            # keep updated set of unvisited edges
            edges_unvisited = o.edges_use[n].copy()
            cur_edges = o.info[n].edges[edges_unvisited, :]
            # stack to return to nodes when an error has occurred along the current path.
            # second index is to store the half path length of the edge on which the error ocurred.
            # third index, added after paper, is the connected component number for this thing
            next_nodes = np.zeros((nalloc,3))
            next_node_cnt = 0
            # stack to return to branch points when encountered along the current path.
            # error free path length is accumulated as long as cur_nodes stack is not empty.
            cur_nodes = np.zeros((nalloc,1))
            cur_node_cnt = 0;   
            
            while np.any(edges_unvisited):
                # find an end point in the remaining edges
                cur_nodes_hist = np.bincount([n  for e in cur_edges for n in e])
                end_node = np.where(cur_nodes_hist == 1)[0][0]

                # if there are still unvisited edges and none of them only appear
                # once in the edge list, then there must be a cycle. in that case
                # just take any node with edges that have not been visited.
                if end_node is None:
                    end_node = np.where(cur_nodes_hist > 0)[0][0]
                    assert(len(end_node) > 0)

                ncomps = ncomps + 1; # start new component (for current thing)
                # push end node (or starting node) onto the next node stack with zero current path length
                next_node_cnt = next_node_cnt+1
                next_nodes[next_node_cnt,:] = [end_node, 0, ncomps]

                # next node stack keeps track of nodes to continue on after an error has occurred
                while next_node_cnt > 0:
                    # pop the next node stack
                    tmp = next_nodes[next_node_cnt,:]
                    next_node_cnt = next_node_cnt - 1
                    cur_node = tmp[0]
                    cur_efpl = tmp[1]
                    cur_comp = tmp[2]

                    # get the remaining edges out of this node
                    cur_node_edges = np.where(cur_edges==cur_node)[0]

                    # if there are no remaining edges out of this node then 
                    #   we've reached an end point without any more nodes after the last error.
                    # save the half length of the previous edge that was stored on the next_nodes stack.
                    if len(cur_node_edges) == 0:
                        efpl_cnt[pass_] = efpl_cnt[pass_]+1
                        efpl[pass_][efpl_cnt[pass_]] = cur_efpl          
                        # associate this efpl with all edges that were involved in it.
                        # error edges will record the efpl for both components.
                        sel = edges_comps==cur_comp
                        efpl_edges[pass_][n][sel] = cur_efpl
                        selcnt = np.sum(sel,1)
                        assert( (selcnt[0]==1 and selcnt[0]==0) or (selcnt[0]==0 and selcnt[1]==1) )
                        continue

                    # push the onto current node stack with current path length
                    cur_node_cnt = cur_node_cnt+1
                    cur_nodes[cur_node_cnt] = cur_node

                    # error free path length accumulates while cur_nodes stack is not empty
                    while cur_node_cnt > 0:
                        # pop the current node stack
                        cur_node = cur_nodes[cur_node_cnt]
                        cur_node_cnt = cur_node_cnt - 1

                        # get the remaining edges out of this node
                        cur_node_edges = np.where(cur_edges==cur_node)[0]

                        # if there are no remaining edges out of this node, we've reached
                        #   the end of this path, continue to any remaining branch points
                        if len(cur_node_edges) == 0:
                            continue

                        # if this node has more than one edge remaining (branch), 
                        #   then push it back to cur_node stack.
                        if len(cur_node_edges) > 1:
                            cur_node_cnt = cur_node_cnt+1
                            cur_nodes[cur_node_cnt] = cur_node

                        # take the first edge out of this node, get both nodes connected to this edge
                        n1 = cur_edges[cur_node_edges[0],0]
                        n2 = cur_edges[cur_node_edges[0],1]
                        # get the original edge number, should only be one edge
                        e = np.where(np.all(o.info[n].edges == np.array([n1, n2]), axis=1))[0]
                        assert( len(e) == 1 )
                        e = e[0]

                        # figure out which is the other node connected to this edge
                        if cur_node == n1:
                            other_node = n2
                        elif cur_node == n2:
                            other_node = n1
                        else:
                            assert( False )

                        # if introducing randomized errors at edges, then over-ride actual error with randomized error
                        if rand_error_rate[pass_] >= 0:
                            error_occurred = (rands[randcnt] < rand_error_rate[pass_])
                            randcnt = randcnt+1
                        else:
                            error_occurred = checkErrorAtEdge(p,n,n1,n2,e,pass_, edge_split, label_merged, nodes_to_labels)

                        if error_occurred:
                            # optionally add half edge length at error edge
                            # true preserves the total path length, false only counts error-free edges in path length
                            if p.count_half_error_edges:
                                cur_len = o.edge_length[n][e]/2
                            else:
                                cur_len = 0

                            # update current error free path length with half this edge length
                            cur_efpl = cur_efpl + cur_len
                            # push the other node onto the next node stack with half this edge length
                            ncomps = ncomps + 1; # edge starting at other node is new component
                            next_node_cnt = next_node_cnt+1
                            next_nodes[next_node_cnt,:] = [other_node, cur_len, ncomps]

                            # save the connected component number for this edge. error edge, so record both components.
                            edges_comps[0,e] = cur_comp
                            edges_comps[1,e] = ncomps
                        else:
                            # update current error free path length with this edge length
                            cur_efpl = cur_efpl + o.edge_length[n][e]
                            # push other node onto current node stack
                            cur_node_cnt = cur_node_cnt+1
                            cur_nodes[cur_node_cnt] = other_node

                            # save the connected component number for this edge. error free, so same component for both.
                            edges_comps[0,e] = cur_comp
                            edges_comps[1,e] = cur_comp

                        # tally that current edge has been visited, udpate current edges
                        edges_unvisited[e] = False
                        cur_edges = o.info[n].edges[edges_unvisited, :]

                    # add the accumulated error free path length after cur_nodes stack is empty
                    efpl_cnt[pass_] = efpl_cnt[pass_]+1
                    efpl[pass_][efpl_cnt[pass_]] = cur_efpl
                    # associate this efpl with all edges that were involved in it.
                    # error edges will record the efpl for both components.
                    sel = (edges_comps==cur_comp)
                    efpl_edges[pass_][n][sel] = cur_efpl

            # sanity check - verify that total efpl is equal to the path length for this thing
            assert( not p.count_half_error_edges or
            (np.abs(np.sum(efpl[pass_][efpl_thing_ptr[n,pass_]:efpl_cnt[pass_]+1]) - o.path_length_use[n]) < p.tol ))

        
        efpl[pass_] = efpl[pass_][1:efpl_cnt[pass_]+1]

    return (efpl, efpl_thing_ptr, efpl_edges)

## use information on merged labels and split edges from first pass through edges
#   to decide if a split or merger error has occurred at this edge or these nodes.
def checkErrorAtEdge(p,n,n1,n2,e,pass_, edge_split, label_merged, nodes_to_labels):
    # a split error has occurred on this path if this edge is split
    split_error_occurred = edge_split[n][e]

    # a merge error has occurred on this path if either node is involved in a merger.
    n1lbl = nodes_to_labels[n][n1]
    n2lbl = nodes_to_labels[n][n2]
    assert( not (n1lbl == p.empty_label | n2lbl == p.empty_label) )
    # do not count a merger for nodes that fall into background areas, these are counted as splits
    merge_error_occurred = ( ((n1lbl < 0) & label_merged[n1lbl]) | ((n2lbl < 0) & label_merged[n2lbl]) )

    # up to four passes over edges are defined as:
    #   (0) splits only (1) mergers only (2) split or merger errors (3) split and merger errors
    if pass_==0:
        error_occurred = split_error_occurred
    elif pass_==1:
        error_occurred = merge_error_occurred
    elif pass_==2:
        error_occurred = (split_error_occurred | merge_error_occurred)
    elif pass_==3:
        error_occurred = (split_error_occurred & merge_error_occurred)
    else:
        assert( False )

    return error_occurred