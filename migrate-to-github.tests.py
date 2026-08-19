
import csv
import io
import unittest
from unittest.mock import MagicMock, patch, call

from migrate_to_github import JiraToGithubMigrator


def make_migrator(token="ghp_test", owner="ORG", repo="REPO"):
    """Create a migrator instance without hitting the network."""
    with patch.object(JiraToGithubMigrator, "_verify_access"), \
         patch.object(JiraToGithubMigrator, "_fetch_existing_issues", return_value={}):
        return JiraToGithubMigrator(token, owner, repo)


def make_csv(rows: list[dict]) -> str:
    """Serialize a list of dicts to a CSV string."""
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _normalize_row
# ---------------------------------------------------------------------------
class TestNormalizeRow(unittest.TestCase):
    def test_issue_key_alias(self):
        row = {"Issue key": "ESM-1", "title": "T", "Issue Type": "Bug"}
        n = JiraToGithubMigrator._normalize_row(row)
        self.assertEqual(n["Key"], "ESM-1")

    def test_title_maps_to_summary(self):
        row = {"title": "My summary", "Issue key": "ESM-2", "Issue Type": "Task"}
        n = JiraToGithubMigrator._normalize_row(row)
        self.assertEqual(n["Summary"], "My summary")

    def test_body_maps_to_description(self):
        row = {"body": "Some description", "Issue key": "ESM-3", "Issue Type": "Bug"}
        n = JiraToGithubMigrator._normalize_row(row)
        self.assertEqual(n["Description"], "Some description")

    def test_parent_key_alias(self):
        row = {"Parent key": "ESM-10", "Issue key": "ESM-3", "title": "T", "Issue Type": "Task"}
        n = JiraToGithubMigrator._normalize_row(row)
        self.assertEqual(n["Parent"], "ESM-10")

    def test_unknown_columns_preserved(self):
        row = {"Custom field (Foo)": "bar", "Issue key": "ESM-4", "title": "T", "Issue Type": "Task"}
        n = JiraToGithubMigrator._normalize_row(row)
        self.assertEqual(n["Custom field (Foo)"], "bar")


# ---------------------------------------------------------------------------
# jira_markup_to_markdown
# ---------------------------------------------------------------------------
class TestJiraMarkupToMarkdown(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()

    def test_headers(self):
        self.assertEqual(self.m.jira_markup_to_markdown("h1. Title"), "# Title")
        self.assertEqual(self.m.jira_markup_to_markdown("h3. Sub"), "### Sub")

    def test_bold(self):
        self.assertEqual(self.m.jira_markup_to_markdown("*bold*"), "**bold**")

    def test_italic(self):
        self.assertEqual(self.m.jira_markup_to_markdown("_italic_"), "*italic*")

    def test_monospace(self):
        self.assertEqual(self.m.jira_markup_to_markdown("{{code}}"), "`code`")

    def test_unordered_list(self):
        result = self.m.jira_markup_to_markdown("* item one\n** nested")
        self.assertIn("- item one", result)
        self.assertIn("  - nested", result)

    def test_ordered_list(self):
        result = self.m.jira_markup_to_markdown("# first\n# second")
        self.assertIn("1. first", result)
        self.assertIn("1. second", result)

    def test_link(self):
        result = self.m.jira_markup_to_markdown("[GitHub|https://github.com]")
        self.assertEqual(result, "[GitHub](https://github.com)")

    def test_code_block(self):
        result = self.m.jira_markup_to_markdown("{code}\nhello\n{code}")
        self.assertIn("```", result)
        self.assertIn("hello", result)

    def test_empty_string(self):
        self.assertEqual(self.m.jira_markup_to_markdown(""), "")

    def test_none(self):
        self.assertIsNone(self.m.jira_markup_to_markdown(None))


# ---------------------------------------------------------------------------
# _get_issue_type
# ---------------------------------------------------------------------------
class TestGetIssueType(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()

    def test_bug(self):
        self.assertEqual(self.m._get_issue_type({"Type": "Bug"}), "Bug")

    def test_epic_maps_to_feature(self):
        self.assertEqual(self.m._get_issue_type({"Type": "Epic"}), "Feature")

    def test_subtask(self):
        self.assertEqual(self.m._get_issue_type({"Type": "Subtask"}), "Task")

    def test_sub_task_hyphen(self):
        self.assertEqual(self.m._get_issue_type({"Type": "Sub-task"}), "Task")

    def test_unknown_returns_none(self):
        self.assertIsNone(self.m._get_issue_type({"Type": "Unknown"}))

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.m._get_issue_type({}))


# ---------------------------------------------------------------------------
# _collect_issue_links
# ---------------------------------------------------------------------------
class TestCollectIssueLinks(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()

    def test_collects_outward_link(self):
        row = {"Outward issue link (Relates)": "ESM-5"}
        result = self.m._collect_issue_links(row)
        self.assertIn("Outward issue link (Relates)", result)
        self.assertIn("ESM-5", result["Outward issue link (Relates)"])

    def test_collects_inward_link(self):
        row = {"Inward issue link (Blocks)": "ESM-3"}
        result = self.m._collect_issue_links(row)
        self.assertIn("ESM-3", result["Inward issue link (Blocks)"])

    def test_ignores_non_link_columns(self):
        row = {"Summary": "ESM-99", "Status": "Open"}
        result = self.m._collect_issue_links(row)
        self.assertEqual(result, {})

    def test_multiple_keys_in_value(self):
        row = {"Outward issue link (Relates)": "ESM-1, ESM-2"}
        result = self.m._collect_issue_links(row)
        self.assertIn("ESM-1", result["Outward issue link (Relates)"])
        self.assertIn("ESM-2", result["Outward issue link (Relates)"])

    def test_empty_value_ignored(self):
        row = {"Outward issue link (Relates)": ""}
        result = self.m._collect_issue_links(row)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _create_or_update_issue
# ---------------------------------------------------------------------------
class TestCreateOrUpdateIssue(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()

    def _mock_response(self, status=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data or {}
        resp.raise_for_status = MagicMock()
        return resp

    def test_creates_new_issue(self):
        self.m.existing_issues = {}
        new_issue = {"number": 42, "id": 9999, "title": "Fix login bug"}
        self.m.session.post = MagicMock(return_value=self._mock_response(201, new_issue))

        row = {"Key": "ESM-1", "Summary": "Fix login bug", "Type": "Bug", "Description": ""}
        number, created = self.m._create_or_update_issue(row)

        self.assertEqual(number, 42)
        self.assertTrue(created)
        self.assertEqual(self.m.jira_issue_map["ESM-1"], 42)
        self.assertEqual(self.m.jira_issue_id_map["ESM-1"], 9999)

    def test_updates_existing_issue(self):
        existing = {"number": 7, "id": 1111, "title": "Fix login bug"}
        self.m.existing_issues = {"Fix login bug": existing}
        self.m.session.patch = MagicMock(return_value=self._mock_response(200, existing))

        row = {"Key": "ESM-1", "Summary": "Fix login bug", "Type": "Bug", "Description": ""}
        number, created = self.m._create_or_update_issue(row)

        self.assertEqual(number, 7)
        self.assertFalse(created)
        self.m.session.patch.assert_called_once()

    def test_returns_none_on_missing_summary(self):
        row = {"Key": "ESM-1", "Summary": "", "Type": "Bug"}
        number, created = self.m._create_or_update_issue(row)
        self.assertIsNone(number)
        self.assertFalse(created)

    def test_returns_none_on_missing_key(self):
        row = {"Key": "", "Summary": "Something", "Type": "Bug"}
        number, created = self.m._create_or_update_issue(row)
        self.assertIsNone(number)
        self.assertFalse(created)

    def test_handles_post_failure(self):
        self.m.existing_issues = {}
        resp = MagicMock()
        resp.status_code = 422
        resp.text = "Validation Failed"
        self.m.session.post = MagicMock(
            side_effect=__import__('requests').exceptions.HTTPError(response=resp)
        )
        row = {"Key": "ESM-2", "Summary": "New issue", "Type": "Task", "Description": ""}
        number, created = self.m._create_or_update_issue(row)
        self.assertIsNone(number)
        self.assertFalse(created)

    def test_type_included_in_payload(self):
        self.m.existing_issues = {}
        new_issue = {"number": 1, "id": 1, "title": "Story issue"}
        self.m.session.post = MagicMock(return_value=self._mock_response(201, new_issue))

        row = {"Key": "ESM-3", "Summary": "Story issue", "Type": "Story", "Description": ""}
        self.m._create_or_update_issue(row)

        call_kwargs = self.m.session.post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        self.assertEqual(payload.get("type"), "Story")


# ---------------------------------------------------------------------------
# _link_child_issues
# ---------------------------------------------------------------------------
class TestLinkChildIssues(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()
        self.m.jira_issue_map = {"ESM-1": 10, "ESM-2": 20, "ESM-3": 30}
        self.m.jira_issue_id_map = {"ESM-1": 1001, "ESM-2": 2002, "ESM-3": 3003}

    def _mock_ok(self):
        resp = MagicMock()
        resp.status_code = 201
        resp.raise_for_status = MagicMock()
        return resp

    def test_posts_sub_issue(self):
        self.m.session.post = MagicMock(return_value=self._mock_ok())
        self.m._link_child_issues("ESM-1", ["ESM-2"])
        self.m.session.post.assert_called_once()
        payload = self.m.session.post.call_args[1]["json"]
        self.assertEqual(payload["sub_issue_id"], 2002)

    def test_skips_unknown_parent(self):
        self.m.session.post = MagicMock()
        self.m._link_child_issues("ESM-99", ["ESM-2"])
        self.m.session.post.assert_not_called()

    def test_skips_unknown_child(self):
        self.m.session.post = MagicMock()
        self.m._link_child_issues("ESM-1", ["ESM-99"])
        self.m.session.post.assert_not_called()

    def test_silently_ignores_422_duplicate(self):
        import requests as req
        resp = MagicMock()
        resp.status_code = 422
        resp.text = "duplicate"
        self.m.session.post = MagicMock(
            side_effect=req.exceptions.HTTPError(response=resp)
        )
        # Should not raise
        self.m._link_child_issues("ESM-1", ["ESM-2"])

    def test_links_multiple_children(self):
        self.m.session.post = MagicMock(return_value=self._mock_ok())
        self.m._link_child_issues("ESM-1", ["ESM-2", "ESM-3"])
        self.assertEqual(self.m.session.post.call_count, 2)


# ---------------------------------------------------------------------------
# _link_related_issues
# ---------------------------------------------------------------------------
class TestLinkRelatedIssues(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()
        self.m.jira_issue_map = {"ESM-1": 10, "ESM-2": 20}

    def _mock_get(self, body="Existing body"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"body": body}
        resp.raise_for_status = MagicMock()
        return resp

    def _mock_patch(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    def test_appends_related_section(self):
        self.m.session.get = MagicMock(return_value=self._mock_get())
        self.m.session.patch = MagicMock(return_value=self._mock_patch())

        self.m._link_related_issues("ESM-1", {"Outward issue link (Relates)": ["ESM-2"]})

        patch_body = self.m.session.patch.call_args[1]["json"]["body"]
        self.assertIn("## Related Issues", patch_body)
        self.assertIn("#20", patch_body)

    def test_skips_unknown_issue_key(self):
        self.m.session.get = MagicMock()
        self.m._link_related_issues("ESM-99", {"Outward issue link (Relates)": ["ESM-2"]})
        self.m.session.get.assert_not_called()

    def test_strips_existing_related_section(self):
        existing = "Body text\n\n## Related Issues\n- **old:** #5"
        self.m.session.get = MagicMock(return_value=self._mock_get(existing))
        self.m.session.patch = MagicMock(return_value=self._mock_patch())

        self.m._link_related_issues("ESM-1", {"Outward issue link (Relates)": ["ESM-2"]})

        patch_body = self.m.session.patch.call_args[1]["json"]["body"]
        self.assertEqual(patch_body.count("## Related Issues"), 1)
        self.assertNotIn("#5", patch_body)

    def test_skips_patch_when_no_resolvable_links(self):
        self.m.session.get = MagicMock(return_value=self._mock_get())
        self.m.session.patch = MagicMock()

        self.m._link_related_issues("ESM-1", {"Outward issue link (Relates)": ["ESM-99"]})
        self.m.session.patch.assert_not_called()


# ---------------------------------------------------------------------------
# migrate (integration-level, file I/O mocked)
# ---------------------------------------------------------------------------
class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.m = make_migrator()

    def _run_migrate_with_csv(self, rows: list[dict]):
        csv_content = make_csv(rows)
        with patch("builtins.open", unittest.mock.mock_open(read_data=csv_content)), \
             patch("csv.DictReader", return_value=iter(rows)), \
             patch.object(self.m, "_create_or_update_issue", return_value=(1, True)) as mock_create, \
             patch.object(self.m, "_link_child_issues") as mock_link_child, \
             patch.object(self.m, "_link_related_issues") as mock_link_related:
            self.m.migrate("fake.csv")
            return mock_create, mock_link_child, mock_link_related

    def test_calls_create_for_each_row(self):
        rows = [
            {"Issue key": "ESM-1", "title": "Issue one", "Issue Type": "Bug", "body": "", "Parent key": ""},
            {"Issue key": "ESM-2", "title": "Issue two", "Issue Type": "Task", "body": "", "Parent key": ""},
        ]
        mock_create, _, _ = self._run_migrate_with_csv(rows)
        self.assertEqual(mock_create.call_count, 2)

    def test_parent_child_map_built(self):
        rows = [
            {"Issue key": "ESM-1", "title": "Parent", "Issue Type": "Epic", "body": "", "Parent key": ""},
            {"Issue key": "ESM-2", "title": "Child", "Issue Type": "Story", "body": "", "Parent key": "ESM-1"},
        ]
        _, mock_link_child, _ = self._run_migrate_with_csv(rows)
        mock_link_child.assert_called_once()
        args = mock_link_child.call_args[0]
        self.assertEqual(args[0], "ESM-1")
        self.assertIn("ESM-2", args[1])

    def test_related_issues_linked(self):
        rows = [
            {
                "Issue key": "ESM-1", "title": "Issue one", "Issue Type": "Bug",
                "body": "", "Parent key": "",
                "Outward issue link (Relates)": "ESM-2",
            },
        ]
        _, _, mock_link_related = self._run_migrate_with_csv(rows)
        mock_link_related.assert_called_once()

    def test_file_not_found_exits(self):
        with self.assertRaises(SystemExit):
            with patch("builtins.open", side_effect=FileNotFoundError):
                self.m.migrate("nonexistent.csv")

    def test_counts_created_updated_failed(self):
        rows = [
            {"Issue key": "ESM-1", "title": "A", "Issue Type": "Bug", "body": "", "Parent key": ""},
            {"Issue key": "ESM-2", "title": "B", "Issue Type": "Task", "body": "", "Parent key": ""},
            {"Issue key": "ESM-3", "title": "C", "Issue Type": "Story", "body": "", "Parent key": ""},
        ]
        side_effects = [(1, True), (2, False), (None, False)]
        with patch("builtins.open", unittest.mock.mock_open()), \
             patch("csv.DictReader", return_value=iter(rows)), \
             patch.object(self.m, "_create_or_update_issue", side_effect=side_effects), \
             patch.object(self.m, "_link_child_issues"), \
             patch.object(self.m, "_link_related_issues"), \
             patch("builtins.print") as mock_print:
            self.m.migrate("fake.csv")

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("1 created", printed)
        self.assertIn("1 updated", printed)
        self.assertIn("1 failed", printed)


if __name__ == "__main__":
    unittest.main()
