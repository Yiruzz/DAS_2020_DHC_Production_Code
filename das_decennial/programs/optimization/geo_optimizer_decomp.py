from copy import deepcopy

import numpy as np
import scipy.sparse as ss
from das_constants import CC

from programs.optimization.geo_optimizers import ASSERT_TYPE
from programs.optimization.optimizer import findNonzeroCols
from programs.optimization.geo_optimizers import L2GeoOpt
from programs.optimization.maps import toGRBFromStr
from programs.queries.constraints_dpqueries import Constraint, StackedConstraint
from programs.queries.querybase import MultiHistQuery, StubQuery, StackedQuery, SparseKronQuery


class GeoOptimizerDecomp(L2GeoOpt):
    """
    This is the base class for solving the L2 and rounder problems with hierarchical decomposition of the histograms between
    the outer optimization passes, which are called from programs/optimization/sequential_optimizers.py. Hierarchical decomposition
    provides a gain in computational efficiency in cases in which 1) query groups included in each outer optimization pass become
    progressively more granular in consecutive outer optimization passes, and 2) when estimating multiple histograms subject to
    consistency constraints between these histograms for certain query groups, the estimates of the answers for these query groups
    are estimated in the first outer optimization pass.
        Inputs:
            single_sub_model: Whether to separate the variables of the optimization problem into multiple sub-models
    """

    single_sub_model: bool
    # Only True when self.child_groups is not None and sequential_optimizer is in the first outer optimization pass. If we are in
    # the first outer optimization pass and self.child_groups is not None, then we have not fixed the total populations of the two
    # state level geounits that are within an individual state. This class assumes that all constraints are in self.constraints, other
    # than possibly parent consistency constraints and non-negativity constraints, and that every such constraint for a given child
    # geounit can be specified using only the main variables of that child geounit. When self.child_groups is not None and
    # sequential_optimizer is in the first outer optimization pass, these assumptions do not hold.

    def __init__(self, *, single_sub_model=False, **kwargs):
        super().__init__(**kwargs)
        self.single_sub_model = single_sub_model
        self.final_solution = np.array([])  # numpy array containing final estimate
        presolve_config = self.getint(CC.PRESOLVE, section=CC.GUROBI, default=-1)
        assert presolve_config > 0, f"Histogram decomposition requires presolve parameter in section {CC.GUROBI} to be 1 or 2 rather than {presolve_config}"

    def setUpConstraints(self, model, two_d_vars, n_list, parent_mask, parent_sub, child_floor):
        # Build other constraints
        if self.constraints is not None:
            self.constraints, self.sub_models = self.removeRedundantConstrsAndFindSubModels(parent_mask, self.constraints, self.single_sub_model)
        else:
            # TODO: Add support histogram decomposition in this case as well:
            self.sub_models = [np.arange(sum(parent_mask), dtype=np.int64)]
        # Add constraints on joined children (e.g. so that AIAN and non-AIAN areas totals sum to the invariant state total)
        if (self.child_groups is not None) and self.single_sub_model:
            # Note that satisfying constraints in earlier outer passes are sufficient to ensure grouped child total constraints hold.
            self.addGroupedChildTotalConstraint(model, self.hist_sizes[0], two_d_vars,
                                                child_groups=self.child_groups, main_n=n_list[0], rounder=self.rounder, child_floor=child_floor)

    def buildObjFxnAddQueries(self, model, two_d_vars, n_list, child_sub, parent_mask, **kwargs):
        return None

    def addStackedConstraints(self, model, parent_mask, two_d_vars, child_floor=None, sub_model_comps=None, sub_model_index=None) -> None:
        """
        Wrapper function that adds constraints to the model
        Inputs:
            model:  gurobi model object
            parent_mask: numpy 1-d boolean array indicating (w/ True) the indexes which then correspond to the children
                         that should be included as variables in the model (i.e. possible non-zero cells of the within the child joint histogram array)
            two_d_vars: a two-dimensional (variables per geography, number of child geographies) gurobi tuplelist variable object
            child_floor: a 2D numpy array used in the rounder (joint histogram array size X number of children)
            sub_model_comps: a list of components (constraints and variables) of the sub-model; variables and constraints being added to the sub-model will be appended to this list
            sub_model_index: an integer index of the sub-model
        """
        import gurobipy as gb
        ASSERT_TYPE(model, gb.Model)
        for constr in self.constraints:
            self.addConstraint(constr, model, parent_mask, two_d_vars, self.rounder, child_floor, sub_model_comps, self.sub_models[sub_model_index])

    @staticmethod
    def addConstraint(st_con: StackedConstraint, model, parent_mask, two_d_vars, rounder=False, child_floor=None, sub_model_comps=None, sub_model_vars=None) -> None:
        """
        Adds stacked constraints to the model
        Inputs:
            st_con: StackedConstraint object (see constraints_dpqueries.py)
            model:  gurobi model object
            parent_mask: numpy 1-d boolean array indicating (w/ True) the indexes which then correspond to the children
                         that should be included as variables in the model (i.e. possible non-zero cells of the within the child joint histogram array)
            two_d_vars: a two-dimensional (variables per geography, number of child geographies) gurobi tuplelist variable object
            rounder: bool indicating if it's the Rounder or L2Opt
            child_floor: a 2D numpy array used in the rounder (joint histogram array size X number of children)
            sub_model_comps: a list of components (constraints and variables) of the sub-model; variables and constraints being added to the sub-model will be appended to this list
            sub_model_vars: a list of integer variable indices indicating the elements of two_d_vars[:, k] that are included in the sub-model for each child k
        """
        import gurobipy as gb
        ASSERT_TYPE(model, gb.Model)

        matrix_rep = st_con.query.matrixRep()[:, parent_mask]
        sense = toGRBFromStr()[st_con.sign]

        row_mask = np.isin(list(range(matrix_rep.shape[0])), np.unique(matrix_rep[:, sub_model_vars].nonzero()[0]), assume_unique=True)

        # Some constraints only appear in a subset of children. stacked_constraint.indices are indices of those children
        for rhs, child_num in zip(st_con.rhsList, st_con.indices):
            if rounder:  # Make constraint to work on the leftovers
                # Find the right-hand side for leftovers
                accounted4byl2opt = st_con.query.answer(child_floor[:, child_num])  # Accounted for by L2 Optimizer
                rhs = rhs - accounted4byl2opt                                       # Left for the Rounder

            # Set a constraint for each value in query answer
            # In the shape Ax=b (appropriate sense instead of '=')
            # addMConstrs was removed in Gurobi 10. addMConstr takes the same arguments but
            # returns one MConstr rather than a list of Constr, hence tolist() -- the same
            # idiom already used at l2_dataIndep_npass_optimizer_decomp.py:255,257.
            constrs = model.addMConstr(A=matrix_rep[row_mask, :], x=two_d_vars[:, child_num], sense=sense, b=rhs[row_mask], name=st_con.name)
            sub_model_comps.extend(constrs.tolist())

    def removeRedundantConstrsAndFindSubModels(self, parent_mask, constraints, single_sub_model):
        # Note that this function will not remove some less-obviously-redundant constraints. For example,
        # only equality constraints are checked to see if they make a constraint redundant. Also, this
        # function only checks each pair of constraint groups to make sure one does not make the other redundant.

        print(f"Input constraints of removeRedundantConstrsAndFindSubModels for {self.identifier}:\n{[(constr.name, constr.query.kronFactors()) for constr in constraints]}")
        skip_decomp = self.getboolean(CC.SKIP_DECOMP, section=CC.GUROBI, default=False)
        constraints, constr_matrix_has_neg_elements = self.removeConstrsWithElementsEqualToNegativeOne(parent_mask, constraints)
        num_constrs = len(constraints)
        single_sub_model = single_sub_model or constr_matrix_has_neg_elements or skip_decomp

        constrs_mask = np.repeat(True, num_constrs)
        for ind1, constr1 in enumerate(constraints[:-1]):
            for k, constr2 in enumerate(constraints[ind1 + 1:]):
                ind2 = ind1 + k + 1
                if (not constrs_mask[ind1]) or (not constrs_mask[ind2]):
                    continue
                if self.checkSufficientConditionsForFirstImplyingSecond(constr1, constr2):
                    constrs_mask[ind2] = False
                elif self.checkSufficientConditionsForFirstImplyingSecond(constr2, constr1):
                    constrs_mask[ind1] = False

        removed_constrs = [constr.name for k, constr in enumerate(constraints) if not constrs_mask[k]]
        remaining_constrs = [constr for k, constr in enumerate(constraints) if constrs_mask[k]]
        print(f"\nThe following constraints were removed for {self.identifier}:\n{removed_constrs}\nThe following constraints were not removed:\n{[constr.name for constr in remaining_constrs]}\n")
        return remaining_constrs, self.findSubModels(remaining_constrs, parent_mask, single_sub_model)

    @staticmethod
    def removeConstrsWithElementsEqualToNegativeOne(parent_mask, constraints):
        # Suppose x and y are vectors of counts from two histograms. When there exists one set of constraints of the form A * x - B * y = a,
        # and the second set of constraints C * x = c, where each row of A can be derived as a sum of the rows of C, (respectively, C * y = c,
        # where each row of B can be derived as a sum of the rows of C) this function replaces A * x - B * y = a with B * y = d, where
        # d := A * x - a, (respectively, A * x = d, where d := a + B * y). Note that we require all nonzero coefficients of A, B, and C to be
        # equal to one. This notation is used for the corresponding variable names below.
        constr_inds_to_remove = []
        constrs_to_add = []
        num_constr_mats_with_neg_one_not_removed = 0
        for k, constr1 in enumerate(constraints):
            cooQ1 = constr1.query.matrixRep()[:, parent_mask].tocoo()
            is_neg_one_mask = cooQ1.data == -1
            num_neg_one = np.sum(is_neg_one_mask)
            if num_neg_one > 0:
                # Add one to num_constr_mats_with_neg_one_not_removed, and we will subtract it back off after we find a constraint that allows us to replace constr1:
                num_constr_mats_with_neg_one_not_removed += 1

                # Define Kronecker factors lists for matrices A and B, which are defined above:
                sense1 = toGRBFromStr()[constr1.sign]
                assert sense1 == "=", "Cannot impose an inequality constraint with an element in its coefficient matrix equal to negative one."
                is_one_mask = np.invert(is_neg_one_mask)
                dataA1 = cooQ1.data[is_one_mask]
                assert np.all(dataA1 == 1)
                csrA = ss.coo_matrix((dataA1, (cooQ1.row[is_one_mask], cooQ1.col[is_one_mask])), shape=cooQ1.shape).tocsr()
                csrB = ss.coo_matrix((np.ones(num_neg_one), (cooQ1.row[is_neg_one_mask], cooQ1.col[is_neg_one_mask])), shape=cooQ1.shape).tocsr()

                kron_facs1 = constr1.query.kronFactors()
                kron_facsA = deepcopy(kron_facs1)
                kron_facsB = deepcopy(kron_facs1)
                # kron_facs1 corresponds to a matrix given by ss.hstack((A, B)), where A and B are defined by the Kronecker factors kron_facs1[0] and kron_facs2[1],
                # respectively. Define kron_facsA and kron_facsB so that the matrices corresponding to these Kronecker factor lists are given by
                # ss.hstack((A, ss.coo_matrix(([], ([], [])), shape=B.shape))) and ss.hstack((ss.coo_matrix(([], ([], [])), shape=A.shape), B)) respectively.
                kron_facsA[1] = [None]
                kron_facsB[0] = [None]

                # Iterate through constraints to find the constraint C * x = d (respectively, C * y = d):
                for i, constr2 in enumerate(constraints):
                    if (i == k) or (i in constr_inds_to_remove):
                        continue
                    # The constraint C * x = d (respectively, C * y = d) is an equality constraint:
                    sense2 = toGRBFromStr()[constr2.sign]
                    if sense2 != "=":
                        continue
                    # The children constrained by C * x = d (respectively, C * y = d) includes all of those that are constrained by A * x - B * y = a:
                    if not np.all(np.isin(constr1.indices, constr2.indices)):
                        continue
                    # All nonzero elements of C are 1:
                    csrC = constr2.query.matrixRep()[:, parent_mask].tocsr()
                    if np.any(np.array(constr2.query.coeff) == -1):  # np.any(csrC.data == -1):
                        continue
                    assert np.all(csrC.data == 1)
                    # C coarsens either A or B:
                    kron_facsC = constr2.query.kronFactors()

                    A_coarsens_C = secondCoarsensFirstUsingKronFactors(kron_facsC, kron_facsA)
                    B_coarsens_C = secondCoarsensFirstUsingKronFactors(kron_facsC, kron_facsB)
                    if (not A_coarsens_C) and (not B_coarsens_C):
                        continue

                    # TODO: add row indices of the Kronecker factors of C required to derive the Kronecker factors of A and B to the output of
                    # the previous calls to secondCoarsensFirstUsingKronFactors to avoid the next two calls, which are more costly:
                    A_coarsens_C, rows_of_C_needed_for_levels_of_A = secondCoarsensFirst(csrC, csrA)
                    B_coarsens_C, rows_of_C_needed_for_levels_of_B = secondCoarsensFirst(csrC, csrB)

                    # Replace constr1:
                    assert constr1.query.coeff == (1, -1)
                    queries = [StubQuery((q.domainSize(), q.numAnswers()), "stub") for q in constr1.query.queries]
                    list_rhs_a_values = constr1.rhsList
                    list_rhs_c_values = constr2.rhsList
                    constrs_indices_equal = []
                    name = f"{constr1.name}_replacement_using_{constr2.name}"
                    if A_coarsens_C:
                        # Replace constr1 with B * y = d, where d := A * x - a:
                        coeff = (0, 1)
                        queries[1] = constr1.query.queries[1]
                        multi_query = MultiHistQuery(tuple(queries), coeff, name)
                        print(f"In removeConstrsWithElementsEqualToNegativeOne: rows_of_C_needed_for_levels_of_A: {rows_of_C_needed_for_levels_of_A}")
                        for sc_child_ind, child_num in enumerate(constr1.indices):
                            Ax_rhs = np.array([np.sum(list_rhs_c_values[child_num][rows_of_C_needed_for_levels_of_A[index_of_c]]) for index_of_c in range(csrA.shape[0])])
                            rhs_d = Ax_rhs - list_rhs_a_values[child_num]
                            constrs_indices_equal.append((Constraint(multi_query, rhs_d, "=", name + f"_child_{child_num}"), child_num))
                        constrs_to_add.append(StackedConstraint(constrs_indices_equal))
                    else:
                        # Replace constr1 with A * x = d, where d := a + B * y:
                        coeff = (1, 0)
                        queries[0] = constr1.query.queries[0]
                        multi_query = MultiHistQuery(tuple(queries), coeff, name)
                        print(f"In removeConstrsWithElementsEqualToNegativeOne: rows_of_C_needed_for_levels_of_B: {rows_of_C_needed_for_levels_of_B}")
                        for sc_child_ind, child_num in enumerate(constr1.indices):
                            By_rhs = np.array([np.sum(list_rhs_c_values[child_num][rows_of_C_needed_for_levels_of_B[index_of_c]]) for index_of_c in range(csrB.shape[0])])
                            rhs_d = By_rhs + list_rhs_a_values[child_num]
                            constrs_indices_equal.append((Constraint(multi_query, rhs_d, "=", name + f"_child_{child_num}"), child_num))
                        constrs_to_add.append(StackedConstraint(constrs_indices_equal))
                    constr_inds_to_remove.append(k)
                    num_constr_mats_with_neg_one_not_removed -= 1

                    print(f"In removeConstrsWithElementsEqualToNegativeOne: found constr2 {constr2.name} that allows for constr1 {constr1.name} to be replaced by {name}.")
                    print(f"In removeConstrsWithElementsEqualToNegativeOne: {constr1.name} has coeff: {constr1.query.coeff}\nKronecker Factors: {constr1.query.kronFactors()}\nRHS lists: {constr1.rhsList}\n")
                    print(f"In removeConstrsWithElementsEqualToNegativeOne: {constr2.name} has coeff: {constr2.query.coeff}\nKronecker Factors: {constr2.query.kronFactors()}\nRHS lists: {constr2.rhsList}\n")
                    print(f"In removeConstrsWithElementsEqualToNegativeOne: constr replacing constr1 is {name}, which has:\ncoeff: {coeff}\nKronecker Factors: {queries}\nand RHS lists: {constrs_to_add[-1].rhsList}\n")
                    break

            if num_constr_mats_with_neg_one_not_removed > 0:
                return constraints, True
        constraints = [constr for k, constr in enumerate(constraints) if k not in constr_inds_to_remove] + constrs_to_add
        return constraints, False

    def findSubModels(self, remaining_constrs, parent_mask, single_sub_model):
        nnzs = int(np.sum(parent_mask))
        if single_sub_model:
            print(f"\nIn {self.identifier}, Final lengths of sub_models: {[nnzs]}\n")
            return [np.arange(nnzs, dtype=np.int64)]

        full_sub_model_basis = self.applyFunctionToConstrAndQueryMatrices(combineRows, remaining_constrs)[:, parent_mask]
        sub_models = [findNonzeroCols(full_sub_model_basis, row) for row in range(full_sub_model_basis.shape[0])]
        sub_models = [sub_model for sub_model in sub_models if len(sub_model) > 0]
        print(f"\nIn {self.identifier}, Final lengths of sub_models: {[len(x) for x in sub_models]}\n")
        return sub_models

    @staticmethod
    def checkSufficientConditionsForFirstImplyingSecond(constr1, constr2):
        # This function checks conditions that are sufficient (but far from necessary) for constr1 implying constr2. For example,
        # this function will only return True if constr1 is an equality constraint.
        sense1 = toGRBFromStr()[constr1.sign]

        # Only return True if constr1 is an equality constraint:
        if sense1 != "=":
            return False

        # Only return True if each element of the constraint matrix of constr1 and constr2 are either zero or one:
        if (not np.all(np.isin(constr1.query.coeff, (0, 1)))) or (not np.all(np.isin(constr2.query.coeff, (0, 1)))):
            return False

        # Only return True if children constrained by constr2 is a subset of those constrained by constr1:
        if not np.all(np.isin(constr2.indices, constr1.indices)):
            return False

        # Only return True if each row of matrix_rep2 can be derived as a sum of rows from matrix_rep1:
        kron_facs1 = constr1.query.kronFactors()
        kron_facs2 = constr2.query.kronFactors()
        if not secondCoarsensFirstUsingKronFactors(kron_facs1, kron_facs2):
            return False
        return True

    def applyFunctionToConstrAndQueryMatrices(self, function, constrs):
        n_hists = len(self.DPqueries)
        cur_kron_facs = [[None] for _ in range(n_hists)]

        for constr in constrs:
            constr_kron_facs = constr.query.kronFactors()
            cur_kron_facs = applyFunctionToRows(cur_kron_facs, constr_kron_facs, function)
        print(f"\nIn {self.identifier}, Kronecker factors for each histogram after applying function to constraint matrices:\n{cur_kron_facs}\n")

        for ihist, dp_queries in enumerate(self.DPqueries):
            if dp_queries is None:
                continue
            for st_dpq in dp_queries:
                query = st_dpq.query
                # Initialize kron_facs so that each histogram has the Kronecker factors of a stub query:
                kron_facs = [[None] for _ in range(n_hists)]
                # Redefine the Kronecker factors of histogram ihist:
                kron_facs[ihist] = query.kronFactors()
                cur_kron_facs = applyFunctionToRows(cur_kron_facs, kron_facs, function)

        print(f"\nIn {self.identifier}, Kronecker factors for each histogram after applying function to DP query matrices:\n{cur_kron_facs}\n")
        supporting_queries = []
        for k in range(n_hists):
            if cur_kron_facs[k][0] is None:
                continue
            query_k = SparseKronQuery(cur_kron_facs[k])
            queries = [StubQuery((q.domainSize(), query_k.numAnswers()), "stub") for q in self.constraints[0].query.queries]
            queries[k] = query_k
            coeff = [0] * n_hists
            coeff[k] = 1
            supporting_queries.append(MultiHistQuery(tuple(queries), tuple(coeff), f"query_mat_for_hist_{k}"))
        return StackedQuery(supporting_queries).matrixRep()

    def findCoarsestSupportingQueryMat(self, parent_mask):
        # This function goes through the query matrices in the constraints and the DP queries and finds the query matrix with the fewest
        # number of rows that supports all of these query matrices.
        query_mat = self.applyFunctionToConstrAndQueryMatrices(createMinimalBasis, self.constraints)[:, parent_mask]

        # drop all rows without nonzero elements:
        row_inds = query_mat.nonzero()[0]
        row_mask = np.isin(np.arange(query_mat.shape[0], dtype=np.int64), row_inds)
        return query_mat[row_mask, :]


def applyFunctionToRows(cur_kron_facs, kron_facs, function):
    # This function is designed to take lists, with elements providing the Kronecker factors of each histogram, as inputs rather than query objects.
    cur_kron_facs = deepcopy(cur_kron_facs)
    kron_facs = deepcopy(kron_facs)

    # Both query matrices must have the same number of histogram components:
    assert len(cur_kron_facs) == len(kron_facs), f"{cur_kron_facs}\n\n{kron_facs}"

    # If the current query matrix for histogram i is a StubQuery, replace it with the query matrix of kron_facs:
    cur_kron_facs = [cur_kron_fac if (cur_kron_fac[0] is not None) else deepcopy(kron_fac) for cur_kron_fac, kron_fac in zip(cur_kron_facs, kron_facs)]

    # Both inputs should be a list composed of lists containing None or matrices of class csr_matrix:
    cur_kron_facs = [cur_kron_fac if (cur_kron_fac[0] is None) else [ss.csr_matrix(x) for x in cur_kron_fac] for cur_kron_fac in cur_kron_facs]
    kron_facs = [kron_fac if (kron_fac[0] is None) else [ss.csr_matrix(x) for x in kron_fac] for kron_fac in kron_facs]

    for ihist, (kron_facs_histi, cur_kron_facs_histi) in enumerate(zip(kron_facs, cur_kron_facs)):
        for kdim, (k_kron_fac_histi, k_cur_kron_fac_histi) in enumerate(zip(kron_facs_histi, cur_kron_facs_histi)):
            if k_kron_fac_histi is None:
                continue
            # Convert Kronecker factor into a list containing one list per row indicating the indices of the columns that are nonzero in the row:
            cols_in_each_row1 = [findNonzeroCols(k_cur_kron_fac_histi, row) for row in range(k_cur_kron_fac_histi.shape[0])]
            cols_in_each_row2 = [findNonzeroCols(k_kron_fac_histi, row) for row in range(k_kron_fac_histi.shape[0])]
            # Call user-supplied function and ensure no rows are empty:
            cols_in_each_row1 = [col_inds for col_inds in function(deepcopy(cols_in_each_row1), deepcopy(cols_in_each_row2)) if len(col_inds) > 0]
            # Convert Kronecker factor back to a csr matrix:
            n_elms_in_rows = [len(col_inds1) for col_inds1 in cols_in_each_row1]
            indptr = np.cumsum([0] + n_elms_in_rows)
            indices = np.concatenate(cols_in_each_row1)
            cur_kron_facs[ihist][kdim] = ss.csr_matrix((np.repeat(True, len(indices)), indices, indptr), shape=(len(cols_in_each_row1), k_cur_kron_fac_histi.shape[1]))
    return cur_kron_facs

def combineRows(cols_in_each_row1, cols_in_each_row2):
    for col_inds2 in cols_in_each_row2:
        inds_not_in_a_sub_model = np.setdiff1d(col_inds2, np.concatenate(cols_in_each_row1), assume_unique=True)
        if len(inds_not_in_a_sub_model) > 0:
            cols_in_each_row1.append(inds_not_in_a_sub_model)
        in_sub_model = np.array([np.any(np.isin(sub_model, col_inds2, assume_unique=True)) for sub_model in cols_in_each_row1])
        # Combine sub_models if they both depend on one or more identical variables:
        num_sub_models = np.sum(in_sub_model)
        if num_sub_models > 1:
            to_combine = np.nonzero(in_sub_model)[0]
            cols_in_each_row1[to_combine[0]] = np.concatenate([sub_model for k, sub_model in enumerate(cols_in_each_row1) if k in to_combine])
            cols_in_each_row1 = [sub_model for k, sub_model in enumerate(cols_in_each_row1) if k not in to_combine[1:]]
    return cols_in_each_row1

def createMinimalBasis(cols_in_each_row1, cols_in_each_row2):
    # Outputs a matrix in scipy.sparse csr format with rowspace identical to the rowspace of ss.vstack((csrQ1, csrQ2)),
    # and with the fewest possible number of nonzero elements. csrQ1 is assumed to have one nonzero element in each column, and both
    # csrQ1 and csrQ2 are assumed to have all nonzero elements equal to one.

    inds_of_2_not_in_1 = np.setdiff1d(np.unique(np.concatenate(cols_in_each_row2)), np.concatenate(cols_in_each_row1), assume_unique=True)
    if len(inds_of_2_not_in_1) > 0:
        cols_in_each_row1.append(inds_of_2_not_in_1)

    changed = True
    while changed:
        changed = False
        for row1, col_inds1 in enumerate(cols_in_each_row1):
            for row2, col_inds2 in enumerate(cols_in_each_row2):
                cols_inds1_not_in_col_inds2 = np.setdiff1d(col_inds1, col_inds2, assume_unique=True)
                # When the row of the first matrix being considered has nonzero columns in the set of nonzero columns of the row of the second
                # matrix being considered, as well as some that are not in this set, split the nonzero columns from these two sets into
                # distinct rows:
                if len(cols_inds1_not_in_col_inds2) not in (0, len(col_inds1)):
                    intersect_col_inds = np.intersect1d(col_inds1, col_inds2, assume_unique=True)
                    cols_in_each_row1[row1] = cols_inds1_not_in_col_inds2
                    cols_in_each_row1.append(intersect_col_inds)
                    changed = True
                    break
            if changed:
                break
    return cols_in_each_row1

def secondCoarsensFirst(csrQ1, csrQ2):
    # Checks to see that each row in csrQ2 can be defined as a sum of rows in csrQ1. If this is not the case, this function returns (False, None),
    # and if it is it returns (True, rows_of_first_needed_for_rows_of_second), where rows_of_first_needed_for_rows_of_second[i] is a list of row indices of
    # csrQ1 such that summing these row vectors together derives csrQ2[i, :].

    msg = "secondCoarsensFirst() assumes all nonzero elements of constraint matrices are equal to one."
    assert np.all(np.asarray(csrQ1.data) == 1), msg
    assert np.all(np.asarray(csrQ2.data) == 1), msg
    rows_of_first_needed_for_rows_of_second = []
    for row2_ind in range(csrQ2.shape[0]):
        rows_of_first_needed_for_row2 = []
        # Initialize cols2 as the set of nonzero columns of csrQ2[row2_ind, :]:
        cols2 = findNonzeroCols(csrQ2, row2_ind)
        for row1_ind in range(csrQ1.shape[0]):
            # Check if we can already derive csrQ2[row2_ind, :] using the rows of csrQ1 with indices given by rows_of_first_needed_for_row2:
            if len(cols2) == 0:
                break
            cols1 = findNonzeroCols(csrQ1, row1_ind)
            mask = np.isin(cols1, cols2)
            # Check if the nonzero columns of csrQ1[row1_ind, :] are a subset of cols2:
            if np.all(mask):
                # Remove the nonzero columns of csrQ1[row1_ind, :] from cols2 and add row1_ind to the list of row indices of csrQ1 that are required to derive csrQ2[row2_ind, :]:
                cols2 = np.setdiff1d(cols2, cols1)
                rows_of_first_needed_for_row2.append(row1_ind)
        if len(cols2) > 0:
            return False, None
        rows_of_first_needed_for_rows_of_second.append(rows_of_first_needed_for_row2)
    return True, rows_of_first_needed_for_rows_of_second

def secondCoarsensFirstUsingKronFactors(kron_facs1, kron_facs2):
    # kron_facs1 and kron_facs2 have format [kron_facs_hist1, kron_facs_hist2, ...], where each kron_facs_histi is either a list of
    # sparse CSR matrices (when the block of the coefficient matrix corresponding to the histogram i has no nonzero elements) or
    # [None] (when the block of the coefficient matrix corresponding to the histogram i has no nonzero elements).

    for kron_facs1_histi, kron_facs2_histi in zip(kron_facs1, kron_facs2):
        for k_kron_fac1_histi, k_kron_fac2_histi in zip(kron_facs1_histi, kron_facs2_histi):
            if (k_kron_fac1_histi is None) and (k_kron_fac2_histi is None):
                # When the first and the second constraint have a StubQuery on the same histogram, only check the coefficient matrices on the remaining histograms:
                continue
            elif (k_kron_fac1_histi is None) or (k_kron_fac2_histi is None):
                # We assume that only StubQuery objects are used to encode a block of zeros. When only one matrix has a row block without nonzero elements, neither
                # of the matrices' rows can be derived by summing the others:
                return False
            first_kron_supports_second_kron, _ = secondCoarsensFirst(k_kron_fac1_histi, k_kron_fac2_histi)
            if not first_kron_supports_second_kron:
                return False
    return True
