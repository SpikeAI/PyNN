# -*- coding: utf-8 -*-
"""
NEST v2 implementation of the PyNN API.

:copyright: Copyright 2006-2019 by the PyNN team, see AUTHORS.
:license: CeCILL, see LICENSE for details.

"""

import numpy
import nest
import logging
from itertools import repeat
try:
    xrange
except NameError:  # Python 3
    xrange = range
from pyNN import common, errors
from pyNN.space import Space
from . import simulator
from pyNN.random import RandomDistribution
from .standardmodels.synapses import StaticSynapse
from .conversion import make_sli_compatible

logger = logging.getLogger("PyNN")

def listify(obj):
    if isinstance(obj, numpy.ndarray):
        return obj.astype(float).tolist()
    elif numpy.isscalar(obj):
        return float(obj)  # NEST chokes on numpy's float types
    else:
        return obj


class Projection(common.Projection):
    __doc__ = common.Projection.__doc__
    _simulator = simulator
    _static_synapse_class = StaticSynapse

    def __init__(self, presynaptic_population, postsynaptic_population,
                 connector, synapse_type=None, source=None, receptor_type=None,
                 space=Space(), label=None):
        common.Projection.__init__(self, presynaptic_population, postsynaptic_population,
                                   connector, synapse_type, source, receptor_type,
                                   space, label)
        self.nest_synapse_model = self.synapse_type._get_nest_synapse_model()
        self.nest_synapse_label = Projection._nProj
        self.synapse_type._set_tau_minus(self.post.local_cells)
        self._sources = []
        self._connections = None
        # This is used to keep track of common synapse properties (to my
        # knowledge they only become apparent once connections are created
        # within nest --obreitwi, 13-02-14)
        self._common_synapse_properties = {}
        self._common_synapse_property_names = None

        # Create connections
        connector.connect(self)

    def __getitem__(self, i):
        """Return the `i`th connection on the local MPI node."""
        if isinstance(i, int):
            if i < len(self):
                return simulator.Connection(self, i)
            else:
                raise IndexError("%d > %d" % (i, len(self) - 1))
        elif isinstance(i, slice):
            if i.stop < len(self):
                return [simulator.Connection(self, j) for j in range(i.start, i.stop, i.step or 1)]
            else:
                raise IndexError("%d > %d" % (i.stop, len(self) - 1))

    def __len__(self):
        """Return the number of connections on the local MPI node."""
        #Disabling the following method pending resolution of https://github.com/nest/nest-simulator/issues/1085
        #local_nodes = nest.GetNodes([0], local_only=True)[0]
        #local_connections = nest.GetConnections(target=local_nodes,
        #                                        synapse_model=self.nest_synapse_model,
        #                                        synapse_label=self.nest_synapse_label)
        local_connections = self.nest_connections
        return len(local_connections)

    @property
    def nest_connections(self):
        if self._connections is None:
            self._sources = numpy.unique(self._sources)
            if self._sources.size > 0:
                self._connections = nest.GetConnections(self._sources.tolist(),
                                                        synapse_model=self.nest_synapse_model,
                                                        synapse_label=self.nest_synapse_label)
            else:
                self._connections = []
        return self._connections

    @property
    def connections(self):
        """
        Returns an iterator over local connections in this projection, as `Connection` objects.
        """
        return (simulator.Connection(self, i) for i in range(len(self)))

    def _connect(self, rule_params, syn_params):
        """
        Create connections by calling nest. Connect on the presynaptic and postsynaptic population
        with the parameters provided by params.
        """
        if 'tsodyks' in self.nest_synapse_model:
            if self.receptor_type == 'inhibitory':
                param_name = self.post.local_cells[0].celltype.translations['tau_syn_I']['translated_name']
            elif self.receptor_type == 'excitatory':
                param_name = self.post.local_cells[0].celltype.translations['tau_syn_E']['translated_name']
            else:
                raise NotImplementedError()
            syn_params.update({'tau_psc': nest.GetStatus([self.nest_connections[0,1]], param_name)})


        syn_params.update({'synapse_label': self.nest_synapse_label})
        nest.Connect(self.pre.all_cells.astype(int).tolist(),
                     self.post.all_cells.astype(int).tolist(),
                     rule_params, syn_params)
        self._sources = [cid[0] for cid in nest.GetConnections(synapse_model=self.nest_synapse_model,
                                                               synapse_label=self.nest_synapse_label)]

    def _convergent_connect(self, presynaptic_indices, postsynaptic_index,
                            **connection_parameters):
        """
        Connect a neuron to one or more other neurons with a static connection.

        `sources`  -- a 1D array of pre-synaptic cell IDs
        `target`   -- the ID of the post-synaptic cell.

        TO UPDATE
        """
        #logger.debug("Connecting to index %s from %s with %s" % (postsynaptic_index, presynaptic_indices, connection_parameters))
        presynaptic_cells = self.pre.all_cells[presynaptic_indices]
        postsynaptic_cell = self.post[postsynaptic_index]
        assert presynaptic_cells.size == presynaptic_indices.size
        assert len(presynaptic_cells) > 0, presynaptic_cells
        syn_dict = {
            'model': self.nest_synapse_model,
            'synapse_label': self.nest_synapse_label,
        }

        weights = connection_parameters.pop('weight')
        if self.receptor_type == 'inhibitory' and self.post.conductance_based:
            weights *= -1  # NEST wants negative values for inhibitory weights, even if these are conductances
            if "stdp" in self.nest_synapse_model:
                syn_dict["Wmax"] = -1.2345e6  # just some very large negative value to avoid
                                              # NEST complaining about weight and Wmax having different signs
                                              # (see https://github.com/NeuralEnsemble/PyNN/issues/636)
                                              # Will be overwritten below.
                connection_parameters["Wmax"] *= -1
        if hasattr(self.post, "celltype") and hasattr(self.post.celltype, "receptor_scale"):  # this is a bit of a hack
            weights *= self.post.celltype.receptor_scale                                      # needed for the Izhikevich model
        delays = connection_parameters.pop('delay')

        # Create connections, with weights and delays
        # Setting other connection parameters is done afterwards
        if postsynaptic_cell.celltype.standard_receptor_type:
            try:
                if not numpy.isscalar(weights):
                    weights = numpy.array([weights])
                if not numpy.isscalar(delays):
                    delays = numpy.array([delays])
                syn_dict.update({'weight': weights, 'delay': delays})

                if 'tsodyks' in self.nest_synapse_model:
                    if self.receptor_type == 'inhibitory':
                        param_name = self.post.local_cells[0].celltype.translations['tau_syn_I']['translated_name']
                    elif self.receptor_type == 'excitatory':
                        param_name = self.post.local_cells[0].celltype.translations['tau_syn_E']['translated_name']
                    else:
                        raise NotImplementedError()
                    syn_dict.update({'tau_psc': numpy.array([[nest.GetStatus([postsynaptic_cell], param_name)[0]] * len(presynaptic_cells.astype(int).tolist())])})

                nest.Connect(presynaptic_cells.astype(int).tolist(),
                             [int(postsynaptic_cell)],
                             'all_to_all',
                             syn_dict)
            except nest.kernel.NESTError as e:
                errmsg = "%s. presynaptic_cells=%s, postsynaptic_cell=%s, weights=%s, delays=%s, synapse model='%s'" % (
                            e, presynaptic_cells, postsynaptic_cell,
                            weights, delays, self.nest_synapse_model)
                raise errors.ConnectionError(errmsg)
        else:
            receptor_type = postsynaptic_cell.celltype.get_receptor_type(self.receptor_type)
            if numpy.isscalar(weights):
                weights = repeat(weights)
            if numpy.isscalar(delays):
                delays = repeat(delays)
            for pre, w, d in zip(presynaptic_cells, weights, delays):
                syn_dict.update({'weight': w, 'delay': d, 'receptor_type': receptor_type})
                if 'tsodyks' in self.nest_synapse_model:
                   syn_dict.update({'tau_psc': numpy.array([[nest.GetStatus([postsynaptic_cell], param_name)[0]] * len(presynaptic_cells.astype(int).tolist())])})

                nest.Connect([pre], [postsynaptic_cell], 'one_to_one', syn_dict)


        # Book-keeping
        self._connections = None  # reset the caching of the connection list, since this will have to be recalculated
        self._sources.extend(presynaptic_cells)

        # Clean the connection parameters
        connection_parameters.pop('tau_minus', None)  # TODO: set tau_minus on the post-synaptic cells
        connection_parameters.pop('dendritic_delay_fraction', None)
        connection_parameters.pop('w_min_always_zero_in_NEST', None)

        # We need to distinguish between common synapse parameters and local ones
        # We just get the parameters of the first connection (is there an easier way?)
        if self._common_synapse_property_names is None:
            self._identify_common_synapse_properties()

        # Set connection parameters other than weight and delay
        if connection_parameters:
            #logger.debug(connection_parameters)
            sort_indices = numpy.argsort(presynaptic_cells)
            connections = nest.GetConnections(source=numpy.unique(presynaptic_cells.astype(int)).tolist(),
                                              target=[int(postsynaptic_cell)],
                                              synapse_model=self.nest_synapse_model,
                                              synapse_label=self.nest_synapse_label)
            for name, value in connection_parameters.items():
                if name not in self._common_synapse_property_names:
                    value = make_sli_compatible(value)
                    #logger.debug("Setting %s=%s for connections %s" % (name, value, connections))
                    if isinstance(value, numpy.ndarray):
                        # the str() is to work around a bug handling unicode names in SetStatus in NEST 2.4.1 when using Python 2
                        nest.SetStatus(connections, str(name), value[sort_indices].tolist())
                    else:
                        nest.SetStatus(connections, str(name), value)
                else:
                    self._set_common_synapse_property(name, value)

    def _identify_common_synapse_properties(self):
        """
            Use the connection between the sample indices to distinguish
            between local and common synapse properties.
        """
        sample_connection = nest.GetConnections(source=[int(self._sources[0])],
                                                synapse_model=self.nest_synapse_model,
                                                synapse_label=self.nest_synapse_label)[:1]

        local_parameters = nest.GetStatus(sample_connection)[0].keys()
        all_parameters = nest.GetDefaults(self.nest_synapse_model).keys()
        self._common_synapse_property_names = [name for name in all_parameters
                                               if name not in local_parameters]

    def _set_attributes(self, parameter_space):
        parameter_space.evaluate(mask=(slice(None), self.post._mask_local))  # only columns for connections that exist on this machine
        sources = numpy.unique(self._sources).tolist()
        if self._common_synapse_property_names is None:
            self._identify_common_synapse_properties()
        for postsynaptic_cell, connection_parameters in zip(self.post.local_cells,
                                                            parameter_space.columns()):
            connections = nest.GetConnections(source=sources,
                                              target=[postsynaptic_cell],
                                              synapse_model=self.nest_synapse_model,
                                              synapse_label=self.nest_synapse_label)
            if connections:
                source_mask = self.pre.id_to_index([x[0] for x in connections])
                for name, value in connection_parameters.items():
                    if name == "weight" and self.receptor_type == 'inhibitory' and self.post.conductance_based:
                        value *= -1  # NEST uses negative values for inhibitory weights, even if these are conductances
                    if name not in self._common_synapse_property_names:
                        value = make_sli_compatible(value)
                        if len(source_mask) > 1:
                            nest.SetStatus(connections, name, value[source_mask])
                        elif isinstance(value, numpy.ndarray):  # OneToOneConnector
                            nest.SetStatus(connections, name, value[source_mask])
                        else:
                            nest.SetStatus(connections, name, value)
                    else:
                        self._set_common_synapse_property(name, value)

    def _set_common_synapse_property(self, name, value):
        """
            Sets the common synapse property while making sure its value stays
            unique (i.e. it can only be set once).
        """
        if name in self._common_synapse_properties:
            unequal = self._common_synapse_properties[name] != value
            # handle both scalars and numpy ndarray
            if isinstance(unequal, numpy.ndarray):
                raise_error = unequal.any()
            else:
                raise_error = unequal
            if raise_error:
                raise ValueError("{} cannot be heterogeneous "
                        "within a single Projection. Warning: "
                        "Projection was only partially initialized."
                        " Please call sim.nest.reset() to reset "
                        "your network and start over!".format(name))
        if hasattr(value, "__len__"):
            value1 = value[0]
        else:
            value1 = value
        self._common_synapse_properties[name] = value1
        # we delay make_sli_compatible until this late stage so that we can
        # distinguish "parameter is an array consisting of scalar values"
        # (one value per connection) from
        # "parameter is a scalar value containing an array"
        # (one value for the entire projection)
        # In the latter case the value is wrapped in a Sequence object,
        # which is removed by make_sli_compatible
        value2 = make_sli_compatible(value1)
        nest.SetDefaults(self.nest_synapse_model, name, value2)

    #def saveConnections(self, file, gather=True, compatible_output=True):
    #    """
    #    Save connections to file in a format suitable for reading in with a
    #    FromFileConnector.
    #    """
    #    import operator
    #
    #    if isinstance(file, basestring):
    #        file = recording.files.StandardTextFile(file, mode='w')
    #
    #    lines   = nest.GetStatus(self.nest_connections, ('source', 'target', 'weight', 'delay'))
    #    if gather == True and simulator.state.num_processes > 1:
    #        all_lines = { simulator.state.mpi_rank: lines }
    #        all_lines = recording.gather_dict(all_lines)
    #        if simulator.state.mpi_rank == 0:
    #            lines = reduce(operator.add, all_lines.values())
    #    elif simulator.state.num_processes > 1:
    #        file.rename('%s.%d' % (file.name, simulator.state.mpi_rank))
    #    logger.debug("--- Projection[%s].__saveConnections__() ---" % self.label)
    #
    #    if gather == False or simulator.state.mpi_rank == 0:
    #        lines       = numpy.array(lines, dtype=float)
    #        lines[:,2] *= 0.001
    #        if compatible_output:
    #            lines[:,0] = self.pre.id_to_index(lines[:,0])
    #            lines[:,1] = self.post.id_to_index(lines[:,1])
    #        file.write(lines, {'pre' : self.pre.label, 'post' : self.post.label})
    #        file.close()

    def _get_attributes_as_list(self, names):
        nest_names = []
        for name in names:
            if name == 'presynaptic_index':
                nest_names.append('source')
            elif name == 'postsynaptic_index':
                nest_names.append('target')
            else:
                nest_names.append(name)
        values = nest.GetStatus(self.nest_connections, nest_names)
        values = numpy.array(values)  # ought to preserve int type for source, target
        if 'weight' in names:  # other attributes could also have scale factors - need to use translation mechanisms
            scale_factors = numpy.ones(len(names))
            scale_factors[names.index('weight')] = 0.001
            if self.receptor_type == 'inhibitory' and self.post.conductance_based:
                scale_factors[names.index('weight')] *= -1  # NEST uses negative values for inhibitory weights, even if these are conductances
            values *= scale_factors
        if 'presynaptic_index' in names:
            values[:, names.index('presynaptic_index')] = self.pre.id_to_index(values[:, names.index('presynaptic_index')])
        if 'postsynaptic_index' in names:
            values[:, names.index('postsynaptic_index')] = self.post.id_to_index(values[:, names.index('postsynaptic_index')])
        values = values.tolist()
        for i in xrange(len(values)):
            values[i] = tuple(values[i])
        return values

    def _get_attributes_as_arrays(self, names, multiple_synapses='sum'):
        multi_synapse_operation = Projection.MULTI_SYNAPSE_OPERATIONS[multiple_synapses]
        all_values = []
        for attribute_name in names:
            if attribute_name[-1] == "s":  # weights --> weight, delays --> delay
                attribute_name = attribute_name[:-1]
            value_arr = numpy.nan * numpy.ones((self.pre.size, self.post.size))
            connection_attributes = nest.GetStatus(self.nest_connections,
                                                   ('source', 'target', attribute_name))
            for conn in connection_attributes:
                # (offset is always 0,0 for connections created with connect())
                src, tgt, value = conn
                addr = self.pre.id_to_index(src), self.post.id_to_index(tgt)
                if numpy.isnan(value_arr[addr]):
                    value_arr[addr] = value
                else:
                    value_arr[addr] = multi_synapse_operation(value_arr[addr], value)
            if attribute_name == 'weight':
                value_arr *= 0.001
                if self.receptor_type == 'inhibitory' and self.post.conductance_based:
                    value_arr *= -1  # NEST uses negative values for inhibitory weights, even if these are conductances
            all_values.append(value_arr)
        return all_values

    def _set_initial_value_array(self, variable, value):
        local_value = value.evaluate(simplify=True)
        nest.SetStatus(self.nest_connections, variable, local_value)
