#!/usr/bin/env python3
"""
Expansion module integrating with VMware NSX Defender.
"""
import argparse
import base64
import configparser
import datetime
import hashlib
import io
import ipaddress
import json
import logging
import pymisp
import sys
import vt
import zipfile
from urllib import parse
from typing import Any, Dict, List, Optional, Tuple, Union

import tau_clients
from tau_clients import exceptions
from tau_clients import nsx_defender


logger = logging.getLogger("vmware_nsx")
logger.setLevel(logging.DEBUG)

misperrors = {
    "error": "Error",
}

mispattributes = {
    "input": [
        "attachment",
        "malware-sample",
        "url",
        "md5",
        "sha1",
        "sha256",
    ],
    "format": "misp_standard",
}

moduleinfo = {
    "version": "0.2",
    "author": "Jason Zhang, Stefano Ortolani",
    "description": "Enrich a file or URL with VMware NSX Defender",
    "module-type": ["expansion", "hover"],
}

moduleconfig = [
    "analysis_url",             # optional, defaults to hard-coded values
    "analysis_verify_ssl",      # optional, defaults to True
    "analysis_key",             # required
    "analysis_api_token",       # required
    "vt_key",                   # optional
    "misp_url",                 # optional
    "misp_verify_ssl",          # optional, defaults to True
    "misp_key",                 # optional
]

DEFAULT_ZIP_PASSWORD = b"infected"

DEFAULT_ENDPOINT = tau_clients.NSX_DEFENDER_DC_WESTUS

WORKFLOW_COMPLETE_TAG = "workflow:state='complete'"

WORKFLOW_INCOMPLETE_TAG = "workflow:state='incomplete'"

VT_DOWNLOAD_TAG = "vt:download"

GALAXY_ATTACK_PATTERNS_UUID = "c4e851fa-775f-11e7-8163-b774922098cd"


class ResultParser:
    """This is a parser to extract *basic* information from a result dictionary."""

    def __init__(self, techniques_galaxy: Optional[Dict[str, str]] = None):
        """Constructor."""
        self.techniques_galaxy = techniques_galaxy or {}

    def parse(self, analysis_link: str, result: Dict[str, Any]) -> pymisp.MISPEvent:
        """
        Parse the analysis result into a MISP event.

        :param str analysis_link: the analysis link
        :param dict[str, any] result: the JSON returned by the analysis client.
        :rtype: pymisp.MISPEvent
        :return: a MISP event
        """
        misp_event = pymisp.MISPEvent()

        # Add analysis subject info
        if "url" in result["analysis_subject"]:
            o = pymisp.MISPObject("url")
            o.add_attribute("url", result["analysis_subject"]["url"])
        else:
            o = pymisp.MISPObject("file")
            o.add_attribute("md5", type="md5", value=result["analysis_subject"]["md5"])
            o.add_attribute("sha1", type="sha1", value=result["analysis_subject"]["sha1"])
            o.add_attribute("sha256", type="sha256", value=result["analysis_subject"]["sha256"])
            o.add_attribute(
                "mimetype",
                category="Payload delivery",
                type="mime-type",
                value=result["analysis_subject"]["mime_type"]
            )
        misp_event.add_object(o)

        # Add HTTP requests from url analyses
        network_dict = result.get("report", {}).get("analysis", {}).get("network", {})
        for request in network_dict.get("requests", []):
            if not request["url"] and not request["ip"]:
                continue
            o = pymisp.MISPObject(name="http-request")
            o.add_attribute("method", "GET")
            if request["url"]:
                parsed_uri = parse.urlparse(request["url"])
                o.add_attribute("host", parsed_uri.netloc)
                o.add_attribute("uri", request["url"])
            if request["ip"]:
                o.add_attribute("ip-dst", request["ip"])
            misp_event.add_object(o)

        # Add network behaviors from files
        for subject in result.get("report", {}).get("analysis_subjects", []):

            # Add DNS requests
            for dns_query in subject.get("dns_queries", []):
                hostname = dns_query.get("hostname")
                # Skip if it is an IP address
                try:
                    if hostname == "wpad" or hostname == "localhost":
                        continue
                    # Invalid hostname, e.g., hostname: ZLKKJRPY or 2.2.0.10.in-addr.arpa.
                    if "." not in hostname or hostname[-1] == ".":
                        continue
                    _ = ipaddress.ip_address(hostname)
                    continue
                except ValueError:
                    pass

                o = pymisp.MISPObject(name="domain-ip")
                o.add_attribute("hostname", type="hostname", value=hostname)
                for ip in dns_query.get("results", []):
                    o.add_attribute("ip", type="ip-dst", value=ip)

                misp_event.add_object(o)

            # Add HTTP conversations (as network connection and as http request)
            for http_conversation in subject.get("http_conversations", []):
                o = pymisp.MISPObject(name="network-connection")
                o.add_attribute("ip-src", http_conversation["src_ip"])
                o.add_attribute("ip-dst", http_conversation["dst_ip"])
                o.add_attribute("src-port", http_conversation["src_port"])
                o.add_attribute("dst-port", http_conversation["dst_port"])
                o.add_attribute("hostname-dst", http_conversation["dst_host"])
                o.add_attribute("layer3-protocol", "IP")
                o.add_attribute("layer4-protocol", "TCP")
                o.add_attribute("layer7-protocol", "HTTP")
                misp_event.add_object(o)

                method, path, http_version = http_conversation["url"].split(" ")
                if http_conversation["dst_port"] == 80:
                    uri = "http://{}{}".format(http_conversation["dst_host"], path)
                else:
                    uri = "http://{}:{}{}".format(
                        http_conversation["dst_host"],
                        http_conversation["dst_port"],
                        path
                    )
                o = pymisp.MISPObject(name="http-request")
                o.add_attribute("host", http_conversation["dst_host"])
                o.add_attribute("method", method)
                o.add_attribute("uri", uri)
                o.add_attribute("ip-dst", http_conversation["dst_ip"])
                misp_event.add_object(o)

        # Add sandbox info like score and sandbox type
        o = pymisp.MISPObject(name="sandbox-report")
        sandbox_type = "saas" if tau_clients.is_task_hosted(analysis_link) else "on-premise"
        o.add_attribute("score", result["score"])
        o.add_attribute("sandbox-type", sandbox_type)
        o.add_attribute("{}-sandbox".format(sandbox_type), "vmware-nsx-defender")
        o.add_attribute("permalink", analysis_link)
        misp_event.add_object(o)

        # Add behaviors
        # Check if its not empty first, as at least one attribute has to be set for sb-signature object
        if result.get("malicious_activity", []):
            o = pymisp.MISPObject(name="sb-signature")
            o.add_attribute("software", "VMware NSX Defender")
            for activity in result.get("malicious_activity", []):
                a = pymisp.MISPAttribute()
                a.from_dict(type="text", value=activity)
                o.add_attribute("signature", **a)
            misp_event.add_object(o)

        # Add mitre techniques
        for techniques in result.get("activity_to_mitre_techniques", {}).values():
            for technique in techniques:
                for misp_technique_id, misp_technique_name in self.techniques_galaxy.items():
                    if technique["id"].casefold() in misp_technique_id.casefold():
                        # If report details a sub-technique, trust the match
                        # Otherwise trust it only if the MISP technique is not a sub-technique
                        if "." in technique["id"] or "." not in misp_technique_id:
                            misp_event.add_tag(misp_technique_name)
                            break
        return misp_event


def _parse_submission_response(response: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Parse the response from "submit_*" methods.

    :param dict[str, any] response: the client response
    :rtype: tuple(str, list[str])
    :return: the task_uuid and whether the analysis is available
    :raises ValueError: in case of any error
    """
    task_uuid = response.get("task_uuid")
    if not task_uuid:
        raise ValueError("Submission failed, unable to process the data")
    if response.get("score") is not None:
        tags = [WORKFLOW_COMPLETE_TAG]
    else:
        tags = [WORKFLOW_INCOMPLETE_TAG]
    return task_uuid, tags


def _unzip(zipped_data: bytes, password: bytes = DEFAULT_ZIP_PASSWORD) -> bytes:
    """
    Unzip the data.

    :param bytes zipped_data: the zipped data
    :param bytes password: the password
    :rtype: bytes
    :return: the unzipped data
    :raises ValueError: in case of any error
    """
    try:
        data_file_object = io.BytesIO(zipped_data)
        with zipfile.ZipFile(data_file_object) as zip_file:
            sample_hash_name = zip_file.namelist()[0]
            return zip_file.read(sample_hash_name, password)
    except (IOError, ValueError) as e:
        raise ValueError(str(e))


def _download_from_vt(client: vt.Client, file_hash: str) -> bytes:
    """
    Download file from VT.

    :param vt.Client client: the VT client
    :param str file_hash: the file hash
    :rtype: bytes
    :return: the downloaded data
    :raises ValueError: in case of any error
    """
    try:
        buffer = io.BytesIO()
        client.download_file(file_hash, buffer)
        buffer.seek(0, 0)
        return buffer.read()
    except (IOError, vt.APIError) as e:
        raise ValueError(str(e))
    finally:
        # vt.Client likes to free resources at shutdown, and it can be used as context to ease that
        # Since the structure of the module does not play well with how MISP modules are organized
        #   let's play nice and close connections pro-actively (opened by "download_file")
        if client:
            client.close()


def _get_analysis_tags(
    clients: Dict[str, nsx_defender.AnalysisClient],
    task_uuid: str,
) -> List[str]:
    """
    Get the analysis tags of a task.

    :param dict[str, nsx_defender.AnalysisClient] clients: the analysis clients
    :param str task_uuid: the task uuid
    :rtype: list[str]
    :return: the analysis tags
    :raises exceptions.ApiError: in case of client errors
    :raises exceptions.CommunicationError: in case of client communication errors
    """
    client = clients[DEFAULT_ENDPOINT]
    response = client.get_analysis_tags(task_uuid)
    tags = set([])
    for tag in response.get("analysis_tags", []):
        tag_header = None
        tag_type = tag["data"]["type"]
        if tag_type == "av_family":
            tag_header = "av-fam"
        elif tag_type == "av_class":
            tag_header = "av-cls"
        elif tag_type == "lastline_malware":
            tag_header = "nsx"
        if tag_header:
            tags.add("{}:{}".format(tag_header, tag["data"]["value"]))
    return sorted(tags)


def _get_latest_analysis(
    clients: Dict[str, nsx_defender.AnalysisClient],
    file_hash: str,
) -> Optional[str]:
    """
    Get the latest analysis.

    :param dict[str, nsx_defender.AnalysisClient] clients: the analysis clients
    :param str file_hash: the hash of the file
    :rtype: str|None
    :return: the task uuid if present, None otherwise
    :raises exceptions.ApiError: in case of client errors
    :raises exceptions.CommunicationError: in case of client communication errors
    """
    def _parse_expiration(task_info: Dict[str, str]) -> datetime.datetime:
        """
        Parse expiration time of a task

        :param dict[str, str] task_info: the task
        :rtype: datetime.datetime
        :return: the parsed datetime object
        """
        return datetime.datetime.strptime(task_info["expires"], "%Y-%m-%d %H:%M:%S")
    results = []
    for data_center, client in clients.items():
        response = client.query_file_hash(file_hash=file_hash)
        for task in response.get("tasks", []):
            results.append(task)
    if results:
        return sorted(results, key=_parse_expiration)[-1]["task_uuid"]
    else:
        return None


def _get_mitre_techniques_galaxy(misp_client: pymisp.PyMISP) -> Dict[str, str]:
    """
    Get all the MITRE techniques from the MISP galaxy.

    :param pymisp.PyMISP misp_client: the MISP client
    :rtype: dict[str, str]
    :return: all techniques indexed by their id
    """
    galaxy_attack_patterns = misp_client.get_galaxy(
        galaxy=GALAXY_ATTACK_PATTERNS_UUID,
        withCluster=True,
        pythonify=True,
    )
    ret = {}
    for cluster in galaxy_attack_patterns.clusters:
        ret[cluster.value] = cluster.tag_name
    return ret


def introspection() -> Dict[str, Union[str, List[str]]]:
    """
    Implement interface.

    :return: the supported MISP attributes
    :rtype: dict[str, list[str]]
    """
    return mispattributes


def version() -> Dict[str, Union[str, List[str]]]:
    """
    Implement interface.

    :return: the module config inside another dictionary
    :rtype: dict[str, list[str]]
    """
    moduleinfo["config"] = moduleconfig
    return moduleinfo


def handler(q: Union[bool, str] = False) -> Union[bool, Dict[str, Any]]:
    """
    Implement interface.

    :param bool|str q: the input received
    :rtype: bool|dict[str, any]
    """
    if q is False:
        return False

    request = json.loads(q)
    config = request.get("config", {})

    # Load the client to connect to VMware NSX ATA (hard-fail)
    try:
        analysis_url = config.get("analysis_url")
        login_params = {
            "key": config["analysis_key"],
            "api_token": config["analysis_api_token"],
        }
        # If 'analysis_url' is specified we are connecting on-premise
        if analysis_url:
            analysis_clients = {
                DEFAULT_ENDPOINT: nsx_defender.AnalysisClient(
                    api_url=analysis_url,
                    login_params=login_params,
                    verify_ssl=bool(config.get("analysis_verify_ssl", True)),
                )
            }
            logger.info("Connected NSX AnalysisClient to on-premise infrastructure")
        else:
            analysis_clients = {
                data_center: nsx_defender.AnalysisClient(
                    api_url=tau_clients.NSX_DEFENDER_ANALYSIS_URLS[data_center],
                    login_params=login_params,
                    verify_ssl=bool(config.get("analysis_verify_ssl", True)),
                ) for data_center in [
                    tau_clients.NSX_DEFENDER_DC_WESTUS,
                    tau_clients.NSX_DEFENDER_DC_NLEMEA,
                ]
            }
            logger.info("Connected NSX AnalysisClient to hosted infrastructure")
    except KeyError as ke:
        logger.error("Integration with VMware NSX ATA failed to connect: %s", str(ke))
        return {"error": "Error connecting to VMware NSX ATA: {}".format(ke)}

    # Load the client to connect to MISP (soft-fail)
    try:
        misp_client = pymisp.PyMISP(
            url=config["misp_url"],
            key=config["misp_key"],
            ssl=bool(config.get("misp_verify_ssl", True)),
        )
    except (KeyError, pymisp.PyMISPError):
        logger.error("Integration with pyMISP disabled: no MITRE techniques tags")
        misp_client = None

    # Load the client to connect to VT (soft-fail)
    try:
        vt_client = vt.Client(apikey=config["vt_key"])
    except (KeyError, ValueError):
        logger.error("Integration with VT disabled: no automatic download of samples")
        vt_client = None

    # Decode and issue the request
    try:
        if request["attribute"]["type"] == "url":
            sample_url = request["attribute"]["value"]
            response = analysis_clients[DEFAULT_ENDPOINT].submit_url(sample_url)
            task_uuid, tags = _parse_submission_response(response)
        else:
            if request["attribute"]["type"] == "malware-sample":
                # Raise TypeError
                file_data = _unzip(base64.b64decode(request["attribute"]["data"]))
                file_name = request["attribute"]["value"].split("|", 1)[0]
                hash_value = hashlib.sha1(file_data).hexdigest()
            elif request["attribute"]["type"] == "attachment":
                # Raise TypeError
                file_data = base64.b64decode(request["attribute"]["data"])
                file_name = request["attribute"].get("value")
                hash_value = hashlib.sha1(file_data).hexdigest()
            else:
                hash_value = request["attribute"]["value"]
                file_data = None
                file_name = "{}.bin".format(hash_value)
            # Check whether we have a task for that file
            tags = []
            task_uuid = _get_latest_analysis(analysis_clients, hash_value)
            if not task_uuid:
                # If we have no analysis, download the sample from VT
                if not file_data:
                    if not vt_client:
                        raise ValueError("No file available locally and VT is disabled")
                    file_data = _download_from_vt(vt_client, hash_value)
                    tags.append(VT_DOWNLOAD_TAG)
                # ... and submit it (_download_from_vt fails if no sample availabe)
                response = analysis_clients[DEFAULT_ENDPOINT].submit_file(file_data, file_name)
                task_uuid, _tags = _parse_submission_response(response)
                tags.extend(_tags)
    except KeyError as e:
        logger.error("Error parsing input: %s", request["attribute"])
        return {"error": "Error parsing input: {}".format(e)}
    except TypeError as e:
        logger.error("Error decoding input: %s", request["attribute"])
        return {"error": "Error decoding input: {}".format(e)}
    except ValueError as e:
        logger.error("Error processing input: %s", request["attribute"])
        return {"error": "Error processing input: {}".format(e)}
    except (exceptions.CommunicationError, exceptions.ApiError) as e:
        logger.error("Error issuing API call: %s", str(e))
        return {"error": "Error issuing API call: {}".format(e)}
    else:
        analysis_link = tau_clients.get_task_link(
            uuid=task_uuid,
            analysis_url=analysis_clients[DEFAULT_ENDPOINT].base,
            prefer_load_balancer=True,
        )

    # Return partial results if the analysis has yet to terminate
    try:
        tags.extend(_get_analysis_tags(analysis_clients, task_uuid))
        report = analysis_clients[DEFAULT_ENDPOINT].get_result(task_uuid)
    except (exceptions.CommunicationError, exceptions.ApiError) as e:
        logger.error("Error retrieving the report: %s", str(e))
        return {
            "results": {
                "types": "link",
                "categories": ["External analysis"],
                "values": analysis_link,
                "tags": tags,
            }
        }

    # Return the enrichment
    try:
        techniques_galaxy = None
        if misp_client:
            techniques_galaxy = _get_mitre_techniques_galaxy(misp_client)
        result_parser = ResultParser(techniques_galaxy=techniques_galaxy)
        misp_event = result_parser.parse(analysis_link, report)
        for tag in tags:
            if tag not in frozenset([WORKFLOW_COMPLETE_TAG]):
                misp_event.add_tag(tag)
        return {
            "results": {
                key: json.loads(misp_event.to_json())[key]
                for key in ("Attribute", "Object", "Tag")
                if (key in misp_event and misp_event[key])
            }
        }
    except pymisp.PyMISPError as e:
        logger.error("Error parsing the report: %s", str(e))
        return {"error": "Error parsing the report: {}".format(e)}


def main():
    """Main function used to test basic functionalities of the module."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config-file",
        dest="config_file",
        required=True,
        help="the configuration file used for testing",
    )
    parser.add_argument(
        "-t",
        "--test-attachment",
        dest="test_attachment",
        default=None,
        help="the path to a test attachment",
    )
    args = parser.parse_args()
    conf = configparser.ConfigParser()
    conf.read(args.config_file)
    config = {
        "analysis_verify_ssl": conf.getboolean("analysis", "analysis_verify_ssl"),
        "analysis_key": conf.get("analysis", "analysis_key"),
        "analysis_api_token": conf.get("analysis", "analysis_api_token"),
        "vt_key": conf.get("vt", "vt_key"),
        "misp_url": conf.get("misp", "misp_url"),
        "misp_verify_ssl": conf.getboolean("misp", "misp_verify_ssl"),
        "misp_key": conf.get("misp", "misp_key"),
    }

    # TEST 1: submit a URL
    j = json.dumps(
        {
            "config": config,
            "attribute": {
                "type": "url",
                "value": "https://www.google.com",
            }
        }
    )
    print(json.dumps(handler(j), indent=4, sort_keys=True))

    # TEST 2: submit a file attachment
    if args.test_attachment:
        with open(args.test_attachment, "rb") as f:
            data = f.read()
        j = json.dumps(
            {
                "config": config,
                "attribute": {
                    "type": "attachment",
                    "value": "test.docx",
                    "data": base64.b64encode(data).decode("utf-8"),
                }
            }
        )
        print(json.dumps(handler(j), indent=4, sort_keys=True))

    # TEST 3: submit a file hash that is known by NSX ATA
    j = json.dumps(
        {
            "config": config,
            "attribute": {
                "type": "md5",
                "value": "002c56165a0e78369d0e1023ce044bf0",
            }
        }
    )
    print(json.dumps(handler(j), indent=4, sort_keys=True))

    # TEST 4 : submit a file hash that is NOT known byt NSX ATA
    j = json.dumps(
        {
            "config": config,
            "attribute": {
                "type": "sha1",
                "value": "2aac25ecdccf87abf6f1651ef2ffb30fcf732250",
            }
        }
    )
    print(json.dumps(handler(j), indent=4, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
