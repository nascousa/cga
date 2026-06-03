from __future__ import annotations

from backend.integrations.azure_devops import extract_azure_devops_refs


def test_extract_azure_devops_refs_from_work_item_url_and_metadata() -> None:
    refs = extract_azure_devops_refs(
        {
            "source_url": "https://dev.azure.com/contoso/CGA/_workitems/edit/5273",
            "tags": ["pbi:5273"],
            "raw_metadata": {
                "ado": {
                    "organization": "contoso",
                    "project": "CGA",
                    "pbi_id": 5273,
                }
            },
        }
    )

    work_items = [ref for ref in refs if ref.item_type == "work_item"]
    assert len(work_items) == 1
    assert work_items[0].item_id == "5273"
    assert work_items[0].organization == "contoso"
    assert work_items[0].project == "CGA"


def test_extract_azure_devops_pull_request_from_url() -> None:
    refs = extract_azure_devops_refs(
        {
            "source_url": "https://dev.azure.com/contoso/CGA/_git/cga/pullrequest/42",
            "raw_metadata": {},
        }
    )

    pull_requests = [ref for ref in refs if ref.item_type == "pull_request"]
    assert len(pull_requests) == 1
    assert pull_requests[0].item_id == "42"
    assert pull_requests[0].organization == "contoso"
    assert pull_requests[0].project == "CGA"
    assert pull_requests[0].repository == "cga"


def test_extract_azure_devops_pr_metadata_requires_allowed_host() -> None:
    refs = extract_azure_devops_refs(
        {
            "source_url": "https://example.test/activity/1",
            "raw_metadata": {
                "pr": {
                    "url": "https://evil.example.test/redirect?next=https://dev.azure.com/contoso/CGA/_git/cga/pullrequest/42"
                }
            },
        }
    )

    assert [ref for ref in refs if ref.item_type == "pull_request"] == []