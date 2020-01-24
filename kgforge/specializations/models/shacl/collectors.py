#
# Knowledge Graph Forge is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Knowledge Graph Forge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser
# General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Knowledge Graph Forge. If not, see <https://www.gnu.org/licenses/>.

from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Optional, Set

from pyshacl.constraints.core.cardinality_constraints import SH_minCount
from pyshacl.constraints.core.logical_constraints import SH_and, SH_or, SH_xone
from pyshacl.constraints.core.other_constraints import SH_in, SH_hasValue
from pyshacl.constraints.core.value_constraints import SH_class, SH_nodeKind, SH_datatype
from pyshacl.consts import SH_property, SH_node, SH_IRI
from pyshacl.shape import Shape
from rdflib import RDF
from rdflib.term import URIRef

from kgforge.specializations.models.shacl.node_properties import NodeProperties


class Collector(ABC):
    """Collector abstract class

    Depending on the constraint, a set of properties and attributes may be
    collected through the collect function.

    Attributes:
        shape: the Shacl Shape used by the collector
    """

    def __init__(self, shape: Shape) -> None:
        self.shape = shape

    @classmethod
    @abstractmethod
    def constraint(cls) -> URIRef:
        """Returns the Shacl constraint URI of the collector"""
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def collect(cls, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                         Optional[Dict]]:
        """ depending on the constraint, this function will return properties, attributes or
        both.

        Args:
            predecessors: list of nodes that have being traversed, used to break circular
                recursion

        Returns:
            properties, attributes: Tuple(list,dict), the collected properties and attributes
                respectively
        """
        raise NotImplementedError()

    def get_shape_target_classes(self) -> List:
        """Returns a list of target and implicit classes if any of the shape

        Returns:
            list of URIs
        """
        (_, target_classes, implicit_classes, _, _) = self.shape.target()
        target_classes = set(target_classes)
        target_classes.update(set(implicit_classes))
        return list(target_classes)


class HasValueCollector(Collector):
    """This class will collect values in the sh:hasValue constraint as attribute"""
    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.has_value = next(shape.objects(SH_hasValue))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_hasValue

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = dict()
        attrs["values"] = self.has_value
        return None, attrs


class DatatypeCollector(Collector):
    """This class will collect values in the sh:datatype constraint as attribute"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.data_type_rule = next(self.shape.objects(SH_datatype))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_datatype

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = dict()
        attrs["values"] = self.data_type_rule
        return None, attrs


class MinCountCollector(Collector):
    """This class will collect values in the sh:minCount constraint as attribute"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.min_count = next(self.shape.objects(SH_minCount))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_minCount

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = dict()
        attrs["mandatory"] = True if self.min_count.toPython() >= 1 else False
        return None, attrs


class NodeKindCollector(Collector):
    """This class will collect values in the sh:nodeKind constraint as attribute"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.node_kind_rule = next(self.shape.objects(SH_nodeKind))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_nodeKind

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = dict()
        if self.node_kind_rule == SH_IRI:
            attrs["id"] = True
        return None, attrs


class InCollector(Collector):
    """This class will collect values in the sh:in constraint as attribute"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        sg = self.shape.sg.graph
        self.in_list = next(self.shape.objects(SH_in))
        self.in_values = set(sg.items(self.in_list))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_in

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = dict()
        attrs["constraint"] = "in"
        attrs["values"] = [v.toPython() for v in self.in_values]
        return None, attrs


class ClassCollector(Collector):
    """This class will collect values in the sh:class constraint as attribute"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.class_rules = list(self.shape.objects(SH_class))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_class

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        attrs = {
            "path": RDF.type,
            "values":  [v for v in self.class_rules]
        }
        return None, attrs


class NodeCollector(Collector):
    """This class will collect values in the sh:node constraint as properties or
    attributes"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.node_shapes = list(self.shape.objects(SH_node))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_node

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        properties = list()
        attributes = dict()
        for n_shape in self.node_shapes:
            ns = self.shape.get_other_shape(n_shape)
            if ns.node not in predecessors:
                predecessors.add(ns.node)
                p, a = ns.traverse(predecessors)
                properties.extend(p)
                attributes.update(a)
        return properties, attributes


class PropertyCollector(Collector):
    """This class will collect values in the sh:property constraint as properties or
        attributes"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.property_shapes = list(self.shape.objects(SH_property))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_property

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        properties = list()
        types = self.get_shape_target_classes()
        if len(types) > 0:
            attrs = {"path": RDF.type, "values": types, "mandatory": True}
            properties.append(NodeProperties(**attrs))
        for p_shape in self.property_shapes:
            ps = self.shape.get_other_shape(p_shape)
            if ps.node not in predecessors:
                predecessors.add(ps.node)
                props, attrs = ps.traverse(predecessors)
                if ps.path() is not None:
                    attrs["path"] = ps.path()
                    if props:
                        attrs["properties"] = props
                    p = NodeProperties(**attrs)
                    properties.append(p)
        return properties, None


class AndCollector(Collector):
    """This class will collect values in the sh:and constraint as properties"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.and_list = list(self.shape.objects(SH_and))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_and

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        properties = list()
        sg = self.shape.sg.graph
        for and_c in self.and_list:
            and_list = set(sg.items(and_c))
            for and_shape in and_list:
                and_shape = self.shape.get_other_shape(and_shape)
                if and_shape.node not in predecessors:
                    predecessors.add(and_shape.node)
                    p, a = and_shape.traverse(predecessors)
                    if a is not None:
                        if and_shape.path() is not None:
                            a["path"] = and_shape.path()
                        if len(p) > 0:
                            a["properties"] = p
                        node = NodeProperties(**a)
                        properties.append(node)
                    else:
                        properties.extend(p)
        types = self.get_shape_target_classes()
        if len(types) > 0:
            attrs = {"path": RDF.type, "values": types, "mandatory": True}
            properties.append(NodeProperties(**attrs))
        return properties, None


class OrCollector(Collector):
    """This class will collect values in the sh:and constraint as properties"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.or_list = list(self.shape.objects(SH_or))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_or

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        properties = list()
        attributes = dict()
        sg = self.shape.sg.graph
        for or_c in self.or_list:
            or_list = set(sg.items(or_c))
            for or_shape in or_list:
                or_shape = self.shape.get_other_shape(or_shape)
                if or_shape.node not in predecessors:
                    predecessors.add(or_shape.node)
                    p, a = or_shape.traverse(predecessors)
                    if a is not None:
                        if or_shape.path() is not None:
                            a["path"] = or_shape.path()
                            node = NodeProperties(**a)
                            properties.append(node)
                            if len(p) > 0:
                                a["properties"] = p
                        else:
                            merge_dicts(attributes, a)
                    elif p is not None:
                        properties.extend(p)
        types = self.get_shape_target_classes()
        if len(types) > 0:
            attrs = {"path": RDF.type, "values": types, "mandatory": True}
            properties.append(NodeProperties(**attrs))
        attributes["constraint"] = "or"
        return properties, attributes


class XoneCollector(Collector):
    """This class will collect values in the sh:and constraint as properties"""

    def __init__(self, shape: Shape) -> None:
        super().__init__(shape)
        self.xone_list = list(self.shape.objects(SH_xone))

    @classmethod
    def constraint(cls) -> URIRef:
        return SH_xone

    def collect(self, predecessors: Set[URIRef]) -> Tuple[Optional[List[NodeProperties]],
                                                          Optional[Dict]]:
        properties = list()
        attributes = dict()
        sg = self.shape.sg.graph
        for xone_c in self.xone_list:
            xone_list = set(sg.items(xone_c))
            for xone_shape in xone_list:
                xone_shape = self.shape.get_other_shape(xone_shape)
                if xone_shape.node not in predecessors:
                    predecessors.add(xone_shape.node)
                    p, a = xone_shape.traverse(predecessors)
                    if a is not None:
                        if xone_shape.path() is not None:
                            a["path"] = xone_shape.path()
                            node = NodeProperties(**a)
                            properties.append(node)
                            if len(p) > 0:
                                a["properties"] = p
                        else:
                            merge_dicts(attributes, a)
                    elif p is not None:
                        properties.extend(p)
        types = self.get_shape_target_classes()
        if len(types) > 0:
            attrs = {"path": RDF.type, "values": types, "mandatory": True}
            properties.append(NodeProperties(**attrs))
        attributes["constraint"] = "xone"
        return properties, attributes


def merge_dicts(dictionary: Dict, new_dict: Dict):
    for key, values in new_dict.items():
        if isinstance(values, str):
            if key in dictionary:
                dictionary[key].append(values)
            else:
                dictionary[key] = [values]
        if isinstance(values, list):
            if key in dictionary:
                dictionary[key].extend(values)
            else:
                dictionary[key] = list(values)