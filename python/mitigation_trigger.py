#!/usr/bin/env python

import argparse
import json
import radix
import re
import subprocess
from json import JSONDecoder, JSONDecodeError
from netaddr import IPAddress, IPNetwork, IPSet


# returns a generator which seperates the json objects in file
def decode_stacked(document, pos=0, decoder=JSONDecoder()):
    NOT_WHITESPACE = re.compile(r'[^\s]')
    while True:
        match = NOT_WHITESPACE.search(document, pos)
        if not match:
            return
        pos = match.start()

        try:
            obj, pos = decoder.raw_decode(document, pos)
        except JSONDecodeError:
            # do something sensible if there's some error
            raise
        yield obj


# returns a list with json objects, each object corresponds to bgp
# configuration of each router with which artemis is connected
def read_json_file(filename):
    json_data = []
    with open(filename, 'r') as json_file:
        json_stacked_data = json_file.read()
        for obj in decode_stacked(json_stacked_data):
            json_data.append(obj)

    return json_data


# create radix-prefix tree with json data (config-file) of each router
def create_prefix_tree(json_data):
    # Create a new tree
    rtree = radix.Radix()

    # Adding a node returns a RadixNode object. You can create
    # arbitrary members in its 'data' dict to store your data.
    # Each node contains a prefix (which a router anounce)
    # as search value and as data --> (asn, bgp router-id, interface
    # name of super-prefix of that prefix) of router
    for i in json_data:
        prefixes_list = i["prefixes"]
        for j in prefixes_list:
            mask = str(IPAddress(j["mask"]).netmask_bits())
            cidr = j["network"] + "/" + mask

            # find out in which interface name this subprefix match
            interface_name = None
            interfaces_list = i["interfaces"]
            for k in interfaces_list:
                interface_mask = str(IPAddress(k["interface_mask"]).netmask_bits())
                interface_cidr = k["interface_ip"] + "/" + interface_mask
                s1 = IPSet([interface_cidr])
                s2 = IPSet([cidr])
                if s1.issuperset(s2) == True:
                    # we found the interface of the superprefix of current subprefix
                    interface_name = k["interface_name"]
                    break

            # search if prefix already exists in tree
            tmp_node = rtree.search_exact(cidr)
            if tmp_node == None:
                # prefix does not exist
                rnode = rtree.add(cidr)
                rnode.data["data_list"] = []
                rnode.data["data_list"].append(
                    (str(i["origin_as"][0]["asn"]), i["bgp_router_id"][0]["router_id"], interface_name))
            else:
                # prefix exist -> update list
                tmp_node.data["data_list"].append(
                    (str(i["origin_as"][0]["asn"]), i["bgp_router_id"][0]["router_id"], interface_name))

    return rtree


def prefix_deaggregation(hijacked_prefix):
    subnets = list(hijacked_prefix.subnet(hijacked_prefix.prefixlen + 1))
    prefix1_data = [str(subnets[0]), str(subnets[0].network), str(subnets[0].netmask)]
    prefix2_data = [str(subnets[1]), str(subnets[1].network), str(subnets[1].netmask)]
    return prefix1_data, prefix2_data


def mitigate_prefix(hijack_json, json_data, admin_configs):
    hijacked_prefix = IPNetwork(json.loads(hijack_json)["prefix"])
    rtree = create_prefix_tree(json_data)

    if hijacked_prefix.prefixlen == 24:
        ##perform tunnel technique

        # Best-match search will return the longest matching prefix
        # that contains the search term (routing-style lookup)
        rnode = rtree.search_best(str(hijacked_prefix.ip))

        # call mitigation playbook for each
        # tuple in longest prefix match node
        for ttuple in rnode.data["data_list"]:
            host = "target=" + ttuple[0] + ":&" + ttuple[1] + " asn=" + ttuple[0]
            prefixes_str = " pr_cidr=" + str(hijacked_prefix.cidr) + " pr_network=" + str(
                hijacked_prefix.ip) + " pr_netmask=" + str(hijacked_prefix.netmask) + " interface_name=" + ttuple[2]
            cla = host + prefixes_str
            arg = "ansible-playbook -i " + admin_configs["ansible_hosts_file_path"] + " " + admin_configs[
                "tunnel_mitigation_playbook_path"] + " --extra-vars " + "\"" + cla + "\""
            subprocess.call(arg, shell=True)

        tunnel_json_key = ""
        for prefix in list(admin_configs["tunnel_definitions"]["hijacked_prefix"].keys()):
            if IPSet([prefix]).issuperset(IPSet([hijacked_prefix.cidr])):
                ## we found the tunnel configs for this prefix
                tunnel_json_key = prefix
                break

        if tunnel_json_key == "":
            # better call the logger from utils
            # from utils import get_logger
            # log = get_logger() , log.info("...")
            print("Tunnel definition for this prefix does not found")
        else:
            # call tunnel_mitigation_playbook for helper as
            # to redirect traffic into the tunnel
            prefix_key = admin_configs["tunnel_definitions"]["hijacked_prefix"][tunnel_json_key]

            host = "target=" + str(prefix_key["helperAS"]["asn"]) + ":&" + prefix_key["helperAS"][
                "router_id"] + " asn=" + str(prefix_key["helperAS"]["asn"])
            prefixes_str = " pr_cidr=" + str(hijacked_prefix.cidr) + " pr_network=" + str(
                hijacked_prefix.ip) + " pr_netmask=" + str(hijacked_prefix.netmask) + " interface_name=" + \
                           str(prefix_key["helperAS"]["tunnel_interface_name"])
            cla = host + prefixes_str
            arg = "ansible-playbook -i " + admin_configs["ansible_hosts_file_path"] + " " + admin_configs[
                "tunnel_mitigation_playbook_path"] + " --extra-vars " + "\"" + cla + "\""
            subprocess.call(arg, shell=True)

    else:
        ##perform prefix-deaggregation technique

        prefix1_data, prefix2_data = prefix_deaggregation(hijacked_prefix)

        # Best-match search will return the longest matching prefix
        # that contains the search term (routing-style lookup)
        rnode = rtree.search_best(str(hijacked_prefix.ip))

        # call mitigation playbook for each
        # tuple in longest prefix match node
        for ttuple in rnode.data["data_list"]:
            host = "target=" + ttuple[0] + ":&" + ttuple[1] + " asn=" + ttuple[0]
            prefixes_str = " pr1_cidr=" + prefix1_data[0] + " pr1_network=" + prefix1_data[1] + " pr1_netmask=" + \
                           prefix1_data[2] + " pr2_cidr=" + prefix2_data[0] + " pr2_network=" + prefix2_data[
                               1] + " pr2_netmask=" + prefix2_data[2] + " interface_name=" + ttuple[2]
            cla = host + prefixes_str
            arg = "ansible-playbook -i " + admin_configs["ansible_hosts_file_path"] + " " + admin_configs[
                "mitigation_playbook_path"] + " --extra-vars " + "\"" + cla + "\""
            subprocess.call(arg, shell=True)


def main():
    parser = argparse.ArgumentParser(description="test ARTEMIS mitigation")
    parser.add_argument("-i", "--info_hijack", dest="info_hijack", type=str, help="hijack event information",
                        required=True)
    hijack_arg = parser.parse_args()

    with open("/root/admin_configs.json") as json_file:
        admin_configs = json.load(json_file)
        json_data = read_json_file(admin_configs["bgp_results_path"])
        mitigate_prefix(hijack_arg.info_hijack, json_data, admin_configs)


if __name__ == '__main__':
    main()