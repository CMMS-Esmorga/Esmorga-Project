import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock


MODULE_PATH = Path(__file__).with_name("jira-to-github.py")
spec = importlib.util.spec_from_file_location("jira_to_github", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
JiraToGitHubMigrator = module.JiraToGitHubMigrator


def make_migrator():
    return JiraToGitHubMigrator(
        "https://example.atlassian.net",
        "jira@example.com",
        "jira-token",
        "github-token",
        "OWNER",
        "REPO",
    )


def response(status_code=200, data=None, content=b""):
    result = MagicMock()
    result.status_code = status_code
    result.json.return_value = data or {}
    result.content = content
    return result


class TestJiraToGitHubMigrator(unittest.TestCase):
    def test_adf_description_converts_common_formatting(self):
        description = {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": " text"},
                    ],
                },
            ],
        }

        markdown = JiraToGitHubMigrator._adf_to_markdown(description)

        self.assertIn("## Title", markdown)
        self.assertIn("**bold** text", markdown)

    def test_legacy_jira_markup_is_converted(self):
        markdown = JiraToGitHubMigrator._adf_to_markdown(
            "h2. Title\n*bold* _italic_\n{code:python}print('hi'){code}\n[GitHub|https://github.com]"
        )

        self.assertIn("## Title", markdown)
        self.assertIn("**bold** *italic*", markdown)
        self.assertIn("```\nprint('hi')\n```", markdown)
        self.assertIn("[GitHub](https://github.com)", markdown)

    def test_issue_body_includes_source_marker_and_attachment_links(self):
        migrator = make_migrator()
        issue = {
            "key": "ESM-10",
            "fields": {"summary": "Title", "description": None, "labels": ["api"]},
        }

        body = migrator._issue_body(issue, [("diagram.png", "https://example.com/diagram.png")])

        self.assertIn("<!-- jira-migration-key: ESM-10 -->", body)
        self.assertIn("## Attachments", body)
        self.assertIn("[diagram.png](https://example.com/diagram.png)", body)

    def test_issue_body_includes_original_jira_creation_date(self):
        migrator = make_migrator()
        issue = {
            "key": "ESM-10",
            "fields": {
                "summary": "Title",
                "description": None,
                "labels": [],
                "created": "2020-01-02T03:04:05.000+0000",
            },
        }

        body = migrator._issue_body(issue, [])

        self.assertIn("**Original Jira created:** 2020-01-02T03:04:05.000+0000", body)

    def test_custom_fields_are_added_to_issue_body(self):
        migrator = make_migrator()
        migrator.jira_field_names = {"customfield_10001": "Steps to reproduce"}
        migrator.issue_numbers = {"ESM-10": 8}
        migrator.issue_ids = {"ESM-10": 80}
        migrator.github.patch = MagicMock(return_value=response(data={"number": 8, "id": 80}))
        issue = {
            "key": "ESM-10",
            "fields": {
                "summary": "Bug",
                "description": None,
                "attachment": [],
                "customfield_10001": "1. Open the app\n2. Observe failure",
            },
        }

        migrator._create_or_update_issue(issue)

        body = migrator.github.patch.call_args.kwargs["json"]["body"]
        self.assertIn("## Custom Fields", body)
        self.assertIn("**Steps to reproduce:**", body)

    def test_image_attachment_is_rendered_inline(self):
        migrator = make_migrator()

        body = migrator._issue_body(
            {"key": "ESM-10", "fields": {"summary": "Title", "description": None, "labels": []}},
            [("diagram.png", "https://github.com/OWNER/REPO/blob/main/.jira-attachments/ESM-10/diagram.png")],
        )

        self.assertIn("![diagram.png](https://raw.githubusercontent.com/OWNER/REPO/main/", body)

    def test_upload_attachment_skips_existing_content(self):
        migrator = make_migrator()
        migrator.jira.get = MagicMock(return_value=response(content=b"attachment-data"))
        migrator.github.get = MagicMock(return_value=response(data={"sha": "old-sha"}))
        migrator.github.put = MagicMock(return_value=response(data={"content": {}}))

        result = migrator._upload_attachment(
            "ESM-10", {"filename": "diagram one.png", "content": "https://jira.example/attachment/1"}
        )

        self.assertEqual(result[0], "diagram one.png")
        self.assertIn("diagram_one.png", result[1])
        migrator.github.put.assert_not_called()

    def test_upload_attachment_creates_missing_content(self):
        migrator = make_migrator()
        migrator.jira.get = MagicMock(return_value=response(content=b"attachment-data"))
        migrator.github.get = MagicMock(return_value=response(404))
        migrator.github.put = MagicMock(return_value=response(data={"content": {}}))

        migrator._upload_attachment(
            "ESM-10", {"filename": "diagram one.png", "content": "https://jira.example/attachment/1"}
        )

        payload = migrator.github.put.call_args.kwargs["json"]
        self.assertNotIn("sha", payload)
        self.assertEqual(payload["content"], "YXR0YWNobWVudC1kYXRh")

    def test_parent_relationship_uses_github_internal_issue_id(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-1": 12}
        migrator.issue_ids = {"ESM-2": 99}
        migrator.github.post = MagicMock(return_value=response(201))
        issue = {"key": "ESM-2", "fields": {"parent": {"key": "ESM-1"}}}

        migrator._link_parent(issue)

        url = migrator.github.post.call_args.args[0]
        payload = migrator.github.post.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/issues/12/sub_issues"))
        self.assertEqual(payload, {"sub_issue_id": 99})

    def test_epic_link_is_used_as_a_sub_issue_parent(self):
        migrator = make_migrator()
        migrator.epic_link_field_id = "customfield_10014"
        migrator.issue_numbers = {"ESM-1": 12}
        migrator.issue_ids = {"ESM-2": 99}
        migrator.github.post = MagicMock(return_value=response(201))
        issue = {"key": "ESM-2", "fields": {"customfield_10014": "ESM-1"}}

        migrator._link_parent(issue)

        url = migrator.github.post.call_args.args[0]
        self.assertTrue(url.endswith("/issues/12/sub_issues"))

    def test_jira_links_preserve_directional_label_and_target(self):
        issue = {
            "fields": {
                "issuelinks": [
                    {
                        "type": {"outward": "blocks", "inward": "is blocked by"},
                        "outwardIssue": {"key": "ESM-2"},
                    }
                ]
            }
        }

        self.assertEqual(JiraToGitHubMigrator._jira_links(issue), [("blocks", "ESM-2")])

    def test_blocks_relation_creates_a_native_github_dependency(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-1": 10, "ESM-2": 20}
        migrator.issue_ids = {"ESM-1": 100, "ESM-2": 200}
        migrator.github.get = MagicMock(return_value=response(data={"body": "Body"}))
        migrator.github.patch = MagicMock(return_value=response())
        migrator.github.post = MagicMock(return_value=response(201))
        issue = {
            "key": "ESM-1",
            "fields": {"issuelinks": [{
                "type": {"outward": "blocks"}, "outwardIssue": {"key": "ESM-2"},
            }]},
        }

        migrator._link_related_issues(issue)

        dependency_url = migrator.github.post.call_args.args[0]
        dependency_payload = migrator.github.post.call_args.kwargs["json"]
        self.assertTrue(dependency_url.endswith("/issues/20/dependencies/blocked_by"))
        self.assertEqual(dependency_payload, {"issue_id": 100})

    def test_unknown_relation_becomes_relates_to(self):
        self.assertEqual(JiraToGitHubMigrator._github_relationship_type("duplicates"), "relates_to")

    def test_existing_jira_link_is_used_to_match_legacy_migration(self):
        migrator = make_migrator()
        migrator.github.get = MagicMock(side_effect=[
            response(data=[{
                "number": 8,
                "id": 80,
                "title": "Legacy issue",
                "body": "**Original Jira Issue:** [ESM-6](https://corunamobilemakers.atlassian.net/browse/ESM-6)",
            }]),
            response(data=[]),
        ])

        migrator._fetch_existing_issues()

        self.assertEqual(migrator.issue_numbers["ESM-6"], 8)
        self.assertEqual(migrator.issue_ids["ESM-6"], 80)

    def test_existing_title_is_updated_not_created_and_done_is_closed(self):
        migrator = make_migrator()
        migrator.issue_numbers_by_title = {"Existing issue": 8}
        migrator.github.patch = MagicMock(return_value=response(data={"number": 8, "id": 80}))
        issue = {
            "key": "ESM-6",
            "fields": {
                "summary": "Existing issue",
                "description": None,
                "attachment": [],
                "issuetype": {"name": "Epic"},
                "status": {"name": "Done"},
            },
        }

        self.assertTrue(migrator._create_or_update_issue(issue))

        payload = migrator.github.patch.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "Feature")
        self.assertEqual(payload["state"], "closed")

    def test_rejected_status_is_closed(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-6": 8}
        migrator.issue_ids = {"ESM-6": 80}
        migrator.github.patch = MagicMock(return_value=response(data={"number": 8, "id": 80}))
        issue = {
            "key": "ESM-6",
            "fields": {
                "summary": "Rejected issue",
                "description": None,
                "attachment": [],
                "status": {"name": "Rejected"},
            },
        }

        migrator._create_or_update_issue(issue)

        self.assertEqual(migrator.github.patch.call_args.kwargs["json"]["state"], "closed")

    def test_done_status_category_is_closed(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-6": 8}
        migrator.issue_ids = {"ESM-6": 80}
        migrator.github.patch = MagicMock(return_value=response(data={"number": 8, "id": 80}))
        issue = {
            "key": "ESM-6",
            "fields": {
                "summary": "Closed category issue",
                "description": None,
                "attachment": [],
                "status": {"name": "Archived", "statusCategory": {"key": "done"}},
            },
        }

        migrator._create_or_update_issue(issue)

        self.assertEqual(migrator.github.patch.call_args.kwargs["json"]["state"], "closed")

    def test_out_of_scope_status_is_closed_and_labeled(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-6": 8}
        migrator.issue_ids = {"ESM-6": 80}
        migrator.github.patch = MagicMock(return_value=response(data={"number": 8, "id": 80}))
        issue = {
            "key": "ESM-6",
            "fields": {
                "summary": "Out of scope issue",
                "description": None,
                "attachment": [],
                "status": {"name": "OUT OF SCOPE"},
            },
        }

        migrator._create_or_update_issue(issue)

        payload = migrator.github.patch.call_args.kwargs["json"]
        self.assertEqual(payload["state"], "closed")
        self.assertEqual(payload["labels"], ["OutOfScope"])

    def test_dry_run_plans_issue_update_without_writes(self):
        migrator = make_migrator()
        migrator.dry_run = True
        migrator.issue_numbers = {"ESM-6": 8}
        migrator.issue_ids = {"ESM-6": 80}
        migrator.github.get = MagicMock(return_value=response(404))
        migrator.github.patch = MagicMock()
        migrator.github.post = MagicMock()
        migrator.github.put = MagicMock()
        issue = {
            "key": "ESM-6",
            "fields": {
                "summary": "Existing issue",
                "description": None,
                "attachment": [{"id": "1", "filename": "file.txt", "size": 4}],
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
            },
        }

        self.assertTrue(migrator._create_or_update_issue(issue))

        migrator.github.patch.assert_not_called()
        migrator.github.post.assert_not_called()
        migrator.github.put.assert_not_called()
        planned = migrator.report["planned_issue_updates"][0]
        self.assertEqual(planned["attachments"][0]["operation"], "upload")

    def test_unmatched_issue_is_created_to_preserve_history(self):
        migrator = make_migrator()
        migrator.github.post = MagicMock(return_value=response(201, {"number": 9, "id": 90}))
        issue = {"key": "ESM-6", "fields": {"summary": "Missing", "attachment": []}}

        self.assertTrue(migrator._create_or_update_issue(issue))

        self.assertTrue(migrator.github.post.call_args.args[0].endswith("/issues"))

    def test_duplicate_titles_create_a_new_issue_without_touching_an_existing_one(self):
        migrator = make_migrator()
        migrator.issue_numbers_by_title = {"Duplicate": 8}
        migrator.ambiguous_titles = {"Duplicate"}
        migrator.github.patch = MagicMock()
        migrator.github.post = MagicMock(return_value=response(201, {"number": 9, "id": 90}))
        issue = {"key": "ESM-6", "fields": {"summary": "Duplicate", "attachment": []}}

        self.assertTrue(migrator._create_or_update_issue(issue))

        migrator.github.patch.assert_not_called()
        migrator.github.post.assert_called_once()

    def test_title_match_is_claimed_once(self):
        migrator = make_migrator()
        migrator.issue_numbers_by_title = {"Repeated title": 8}
        migrator.issue_ids_by_title = {"Repeated title": 80}
        migrator.github.patch = MagicMock(return_value=response(200, {"number": 8, "id": 80}))
        migrator.github.post = MagicMock(return_value=response(201, {"number": 9, "id": 90}))
        issue = {"key": "ESM-1", "fields": {"summary": "Repeated title", "attachment": []}}

        self.assertTrue(migrator._create_or_update_issue(issue))
        self.assertTrue(migrator._create_or_update_issue({"key": "ESM-2", "fields": {"summary": "Repeated title", "attachment": []}}))

        migrator.github.patch.assert_called_once()
        migrator.github.post.assert_called_once()

    def test_github_write_retries_secondary_rate_limit(self):
        migrator = make_migrator()
        limited = response(403)
        limited.text = '{"message":"You have exceeded a secondary rate limit"}'
        limited.headers = {}
        success = response(201)
        migrator.github.post = MagicMock(side_effect=[limited, success])

        with unittest.mock.patch.object(module.time, "sleep"):
            result = migrator._github_write("POST", "https://api.github.com/test", json={})

        self.assertEqual(result.status_code, 201)
        self.assertEqual(migrator.github.post.call_count, 2)

    def test_github_read_retries_secondary_rate_limit(self):
        migrator = make_migrator()
        limited = response(403)
        limited.text = '{"message":"You have exceeded a secondary rate limit"}'
        limited.headers = {}
        success = response(200)
        migrator.github.get = MagicMock(side_effect=[limited, success])

        with unittest.mock.patch.object(module.time, "sleep"):
            result = migrator._github_read("https://api.github.com/test")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(migrator.github.get.call_count, 2)

    def test_migrates_each_jira_comment_once(self):
        migrator = make_migrator()
        migrator.issue_numbers = {"ESM-6": 8}
        migrator.github.get = MagicMock(return_value=response(data=[]))
        migrator.jira.get = MagicMock(return_value=response(data={
            "comments": [{
                "id": "123",
                "author": {"displayName": "Ada"},
                "created": "2026-01-01T12:00:00.000+0000",
                "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]},
            }],
            "total": 1,
        }))
        migrator.github.post = MagicMock(return_value=response(201))

        migrator._migrate_comments({"key": "ESM-6", "fields": {}})

        body = migrator.github.post.call_args.kwargs["json"]["body"]
        self.assertIn("jira-migration-comment: 123", body)
        self.assertIn("by Ada", body)
        self.assertIn("Hello", body)


if __name__ == "__main__":
    unittest.main()
