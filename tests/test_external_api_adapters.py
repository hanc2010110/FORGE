from __future__ import annotations

import json
import unittest
from typing import Literal, cast

from forge_core.external_api_adapters import (
    ArasReadOnlyClient,
    AutodeskAPSReadOnlyClient,
    ExternalAPIError,
    GitLabReadOnlyClient,
    JenkinsReadOnlyClient,
    OnshapeReadOnlyClient,
    WindchillReadOnlyClient,
    solidworks_3dexperience_descriptor,
    teamcenter_descriptor,
)


class FakeTransport:
    def __init__(self, response: object, *, status: int = 200) -> None:
        self.response = response
        self.status = status
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: Literal["GET"],
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return (
            self.status,
            {"content-type": "application/json"},
            json.dumps(self.response).encode(),
        )


class ExternalAPIAdapterTests(unittest.TestCase):
    def test_gitlab_client_reads_commit_merge_requests_pipelines_and_jobs(self) -> None:
        transport = FakeTransport({"id": "abc1234", "title": "fix safety gate"})
        gitlab = GitLabReadOnlyClient(
            base_url="https://gitlab.example.com",
            project_path="team/robot",
            token="glpat-secret",
            transport=transport,
        )

        commit = gitlab.get_commit(sha="abc1234")
        self.assertEqual(commit["title"], "fix safety gate")
        self.assertIn(
            "/api/v4/projects/team%2Frobot/repository/commits/abc1234",
            str(transport.requests[0]["url"]),
        )

        transport.response = [{"iid": 9, "state": "opened"}]
        merge_requests = gitlab.list_merge_requests(state="opened")
        self.assertEqual(merge_requests[0]["iid"], 9)
        self.assertIn("state=opened", str(transport.requests[1]["url"]))

        transport.response = [{"id": 7, "sha": "a" * 40, "status": "success"}]
        pipelines = gitlab.list_pipelines(ref="main")
        self.assertEqual(pipelines[0]["status"], "success")
        self.assertIn("/pipelines?ref=main", str(transport.requests[2]["url"]))

        transport.response = [{"id": 8, "name": "verify", "status": "success"}]
        jobs = gitlab.list_pipeline_jobs(pipeline_id=7)
        self.assertEqual(jobs[0]["name"], "verify")
        self.assertIn("/pipelines/7/jobs", str(transport.requests[3]["url"]))
        self.assertNotIn("glpat-secret", repr(transport.requests))

    def test_jenkins_client_reads_job_build_last_build_and_test_report(self) -> None:
        transport = FakeTransport({"name": "FORGE/verify", "color": "blue"})
        jenkins = JenkinsReadOnlyClient(
            base_url="https://ci.example.com",
            job_path="FORGE/verify",
            username="forge",
            api_token="jenkins-secret",
            transport=transport,
        )

        job = jenkins.get_job()
        self.assertEqual(job["color"], "blue")
        self.assertIn(
            "/job/FORGE/job/verify/api/json", str(transport.requests[0]["url"])
        )

        transport.response = {"building": False, "result": "SUCCESS"}
        build = jenkins.get_build(build_number=42)
        self.assertEqual(build["result"], "SUCCESS")

        transport.response = {"number": 43, "result": "SUCCESS"}
        last_build = jenkins.get_last_build()
        self.assertEqual(last_build["number"], 43)

        transport.response = {"failCount": 0, "passCount": 128}
        report = jenkins.get_test_report(build_number=43)
        self.assertEqual(report["passCount"], 128)
        self.assertIn("/43/testReport/api/json", str(transport.requests[3]["url"]))
        self.assertNotIn("jenkins-secret", repr(transport.requests))

    def test_onshape_adapter_reads_document_versions_and_assembly(self) -> None:
        transport = FakeTransport({"name": "upper arm", "modifiedAt": "now"})
        onshape = OnshapeReadOnlyClient(
            base_url="https://cad.onshape.com",
            access_token="onshape-secret",
            transport=transport,
        )
        doc = onshape.get_document(document_id="d1")
        self.assertEqual(doc["name"], "upper arm")

        transport.response = [{"id": "v1", "name": "released"}]
        versions = onshape.list_document_versions(document_id="d1")
        self.assertEqual(versions[0]["name"], "released")

        transport.response = {"rootAssembly": {"instances": []}}
        assembly = onshape.get_assembly_definition(
            document_id="d1",
            workspace_or_version="v",
            workspace_or_version_id="v1",
            element_id="e1",
        )
        self.assertIn("rootAssembly", assembly)
        self.assertIn(
            "/api/assemblies/d/d1/v/v1/e/e1", str(transport.requests[2]["url"])
        )
        self.assertNotIn("secret", repr(transport.requests))

    def test_autodesk_windchill_and_aras_read_only_contracts(self) -> None:
        aps_transport = FakeTransport({"data": {"id": "p1"}})
        aps = AutodeskAPSReadOnlyClient(
            base_url="https://developer.api.autodesk.com",
            access_token="aps-secret",
            transport=aps_transport,
        )
        project = aps.get_project(hub_id="h1", project_id="p1")
        self.assertEqual(project["data"], {"id": "p1"})

        aps_transport.response = {"data": [{"id": "v1"}]}
        versions = aps.list_item_versions(
            project_id="p1", item_id="urn:adsk.wip:fs.file:vf.1"
        )
        self.assertEqual(versions[0]["id"], "v1")

        aps_transport.response = {"status": "success"}
        manifest = aps.get_derivative_manifest(urn="dXJuOmFkc2subWlw")
        self.assertEqual(manifest["status"], "success")
        self.assertNotIn("aps-secret", repr(aps_transport.requests))

        windchill_transport = FakeTransport({"Number": "ARM-001"})
        windchill = WindchillReadOnlyClient(
            base_url="https://plm.example.com",
            access_token="windchill-secret",
            transport=windchill_transport,
        )
        part = windchill.get_part(part_oid="OR:wt.part.WTPart:123")
        self.assertEqual(part["Number"], "ARM-001")
        self.assertIn("ProdMgmt/Parts", str(windchill_transport.requests[0]["url"]))
        windchill_transport.response = {"Number": "CN-9"}
        notice = windchill.get_change_notice(
            notice_oid="OR:wt.change2.WTChangeOrder2:9"
        )
        self.assertEqual(notice["Number"], "CN-9")

        aras_transport = FakeTransport({"item_number": "ARM-001"})
        aras = ArasReadOnlyClient(
            base_url="https://aras.example.com",
            access_token="aras-secret",
            transport=aras_transport,
        )
        item = aras.get_item(collection="Part", item_id="ARM-001")
        self.assertEqual(item["item_number"], "ARM-001")
        self.assertIn(
            "/Server/odata/Part('ARM-001')", str(aras_transport.requests[0]["url"])
        )
        self.assertNotIn("aras-secret", repr(aras_transport.requests))

    def test_clients_fail_closed_on_bad_boundaries_and_http_errors(self) -> None:
        with self.assertRaises(ValueError):
            GitLabReadOnlyClient(
                base_url="http://gitlab.example.com",
                project_path="team/robot",
                token="token",
                transport=FakeTransport({}),
            )
        client = GitLabReadOnlyClient(
            base_url="https://gitlab.example.com",
            project_path="team/robot",
            token="secret",
            transport=FakeTransport({"error": "bad"}, status=500),
        )
        with self.assertRaisesRegex(ExternalAPIError, "external_api_http_500"):
            client.list_pipelines(ref="main")
        with self.assertRaises(ValueError):
            client.get_commit(sha="../main")

        jenkins = JenkinsReadOnlyClient(
            base_url="https://ci.example.com",
            job_path="FORGE/verify",
            username="forge",
            api_token="secret",
            transport=FakeTransport({}),
        )
        with self.assertRaises(ValueError):
            jenkins.get_build(build_number=0)
        with self.assertRaises(ValueError):
            ArasReadOnlyClient(
                base_url="https://aras.example.com",
                access_token="secret",
                transport=FakeTransport({}),
            ).get_item(collection="../Part", item_id="ARM-001")

    def test_solidworks_and_teamcenter_remain_honest_unavailable_descriptors(
        self,
    ) -> None:
        solidworks = solidworks_3dexperience_descriptor().model_dump()
        self.assertFalse(solidworks["available"])
        self.assertIn("does not guess", str(solidworks["reason"]))
        solidworks_requirements = cast(list[str], solidworks["required_from_vendor"])
        self.assertIsInstance(solidworks_requirements, list)
        self.assertIn("tenant base URL", solidworks_requirements)

        teamcenter = teamcenter_descriptor().model_dump()
        self.assertFalse(teamcenter["available"])
        self.assertIn("deployment and version specific", str(teamcenter["reason"]))
        teamcenter_requirements = cast(list[str], teamcenter["required_from_vendor"])
        self.assertIsInstance(teamcenter_requirements, list)
        self.assertIn("Teamcenter version", teamcenter_requirements)


if __name__ == "__main__":
    unittest.main()
