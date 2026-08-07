#!/usr/bin/env python3
"""
Migrate Jira issues from CSV to GitHub.
Handles conversion of issue details, attachments, and relationships.
"""

import csv
import re
import os
import sys
import argparse
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
import requests
from requests.auth import HTTPBasicAuth


class JiraToGithubMigrator:
    """Migrate Jira issues to GitHub."""
    
    JIRA_BASE_URL = "https://corunamobilemakers.atlassian.net/"
    JIRA_BROWSE_URL = urljoin(JIRA_BASE_URL, "browse/")
    
    JIRA_TO_GITHUB_TYPE = {
        "Bug": "Bug",
        "Story": "Story",
        "Task": "Task",
        "Epic": "Feature",
        "Sub-task": "Task",
        "Subtask": "Task",
    }
    
    # Map of canonical name -> possible CSV column names (case-insensitive)
    COLUMN_ALIASES = {
        "Key":         ["Key", "Issue key", "Issue Key"],
        "Summary":     ["Summary", "title", "Title"],
        "Description": ["Description", "body", "Body"],
        "Type":        ["Type", "Issue Type", "Issue type"],
        "Status":      ["Status"],
        "Priority":    ["Priority"],
        "Assignee":    ["Assignee"],
        "Reporter":    ["Reporter"],
        "Created":     ["Created"],
        "Updated":     ["Updated"],
        "Parent":      ["Parent key", "Parent issue", "Epic Link", "Parent"],
        "Sprint":      ["Sprint"],
    }

    @classmethod
    def _normalize_row(cls, row: Dict) -> Dict:
        """Remap CSV column names to canonical names used by the rest of the code."""
        lower_row = {k.lower(): v for k, v in row.items()}
        normalized = dict(row)  # keep originals for custom fields
        for canonical, aliases in cls.COLUMN_ALIASES.items():
            for alias in aliases:
                if alias.lower() in lower_row:
                    normalized[canonical] = lower_row[alias.lower()]
                    break
        return normalized

    def __init__(self, github_token: str, owner: str, repo: str):
        """
        Initialize migrator.
        
        Args:
            github_token: GitHub personal access token
            owner: GitHub repository owner
            repo: GitHub repository name
        """
        self.github_token = github_token
        self.owner = owner
        self.repo = repo
        self.github_headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Media-Type": "github.v3",
        }
        self.github_api_base = "https://api.github.com"
        self.session = requests.Session()
        self.session.trust_env = False  # Ignore ~/.netrc and env proxy settings
        self.session.headers.update(self.github_headers)
        self._verify_access()
        self.existing_issues = self._fetch_existing_issues()
        self.jira_issue_map = {}     # Jira key -> GitHub issue number (#N)
        self.jira_issue_id_map = {}  # Jira key -> GitHub internal issue id

    def _verify_access(self):
        """Check API token validity and repository access, exit with a clear message on failure."""
        # 1. Token validity
        try:
            r = self.session.get(f"{self.github_api_base}/user")
            if r.status_code == 401:
                print("Error: GitHub token is invalid or expired. Generate a new token at https://github.com/settings/tokens")
                sys.exit(1)
            r.raise_for_status()
            username = r.json().get("login", "<unknown>")
            print(f"  Authenticated as: {username}")
        except requests.exceptions.RequestException as e:
            print(f"Error: Could not reach GitHub API: {e}")
            sys.exit(1)

        # 2. Org membership (informational)
        r_org = self.session.get(
            f"{self.github_api_base}/orgs/{self.owner}/members/{username}",
        )
        if r_org.status_code == 302:
            print(f"  Org membership: {username} is a public member of {self.owner}")
        elif r_org.status_code == 204:
            print(f"  Org membership: {username} is a member of {self.owner}")
        elif r_org.status_code == 404:
            print(f"  Warning: {username} does not appear to be a member of the '{self.owner}' org.")

        # 3. Repository access
        try:
            r = self.session.get(
                f"{self.github_api_base}/repos/{self.owner}/{self.repo}",
            )
            if r.status_code == 404:
                # Check if the org itself is visible
                r_org_check = self.session.get(
                    f"{self.github_api_base}/orgs/{self.owner}",
                )
                if r_org_check.status_code == 404:
                    print(f"Error: Organization '{self.owner}' not found.")
                else:
                    print(
                        f"Error: Repository '{self.owner}/{self.repo}' not found or not accessible.\n"
                        f"  The organization exists but the repo is not visible with your token.\n"
                        f"  - If the org uses SAML SSO, authorize your token:\n"
                        f"    https://github.com/settings/tokens → 'Configure SSO' → Authorize '{self.owner}'\n"
                        f"  - Ensure the token has the 'repo' scope (not just 'public_repo').\n"
                        f"  - Ensure {username} has at least read access to the repository."
                    )
                sys.exit(1)
            if r.status_code == 403:
                print(
                    f"Error: Access forbidden for '{self.owner}/{self.repo}'.\n"
                    f"  Your token may lack the 'repo' scope, or the organization has\n"
                    f"  IP allow-list / SSO restrictions. Check https://github.com/settings/tokens"
                )
                sys.exit(1)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error: Could not access repository: {e}")
            sys.exit(1)

        repo_data = r.json()
        if not repo_data.get("has_issues", True):
            print(f"Error: Issues are disabled for '{self.owner}/{self.repo}'.")
            sys.exit(1)
    
    def _fetch_existing_issues(self) -> Dict[str, Dict]:
        """Fetch all existing GitHub issues for matching."""
        issues = {}
        page = 1
        
        try:
            while True:
                url = f"{self.github_api_base}/repos/{self.owner}/{self.repo}/issues"
                response = self.session.get(
                    url,
                    params={"state": "all", "per_page": 100, "page": page},
                )
                response.raise_for_status()
                
                data = response.json()
                if not data:
                    break
                
                for issue in data:
                    issues[issue["title"]] = issue
                
                page += 1
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                status = e.response.status_code
                if status == 404:
                    print(
                        f"Warning: Could not fetch existing issues (404 Not Found).\n"
                        f"  Check that the repository '{self.owner}/{self.repo}' exists and that\n"
                        f"  your token has the 'repo' scope (classic) or 'issues: read' permission\n"
                        f"  (fine-grained). Issues will be created fresh without duplicate checking."
                    )
                elif status == 401:
                    print("Warning: Could not fetch existing issues (401 Unauthorized). Check your token.")
                else:
                    print(f"Warning: Could not fetch existing issues ({status}): {e}")
            else:
                print(f"Warning: Could not fetch existing issues: {e}")
        
        return issues
    
    def jira_markup_to_markdown(self, text: str) -> str:
        """Convert Jira markup to GitHub markdown."""
        if not text:
            return text
        
        # Unordered lists: * item, ** item (nested) -> markdown indented list
        # Must be done before bold (*text*) conversion
        text = re.sub(
            r'^(\*+)\s+(.+)$',
            lambda m: '  ' * (len(m.group(1)) - 1) + '- ' + m.group(2),
            text,
            flags=re.MULTILINE,
        )
        
        # Ordered lists: # item, ## item (nested) -> markdown numbered list
        # Must be done before header conversion
        text = re.sub(
            r'^(#+)\s+(.+)$',
            lambda m: '  ' * (len(m.group(1)) - 1) + '1. ' + m.group(2),
            text,
            flags=re.MULTILINE,
        )
        
        # Headers: h1. -> #, h2. -> ##, etc
        text = re.sub(r'h([1-6])\.\s+', lambda m: '#' * int(m.group(1)) + ' ', text)
        
        # Bold: *text* -> **text**
        text = re.sub(r'\*([^*]+)\*', r'**\1**', text)
        
        # Italic: _text_ -> *text*
        text = re.sub(r'_([^_]+)_', r'*\1*', text)
        
        # Monospace: {{text}} -> `text`
        text = re.sub(r'\{\{([^}]+)\}\}', r'`\1`', text)
        
        # Code blocks: {code}...{code} or {code:language}...{code}
        text = re.sub(
            r'\{code(?::[\w]+)?\}(.*?)\{code\}',
            lambda m: f'```\n{m.group(1)}\n```',
            text,
            flags=re.DOTALL,
        )
        
        # Links: [text|url] -> [text](url)
        text = re.sub(r'\[([^\]|]+)\|([^\]]+)\]', r'[\1](\2)', text)
        
        # Simple links: [url] -> [url](url)
        text = re.sub(r'\[(?!.*\]\()(https?://[^\]]+)\]', r'[\1](\1)', text)
        
        # Strikethrough: -text- -> ~~text~~
        text = re.sub(r'-([^-\n]+)-', r'~~\1~~', text)
        
        # Lists: maintain basic structure
        # Jira unordered: * or - at start
        # Jira ordered: # at start
        
        return text.strip()
    
    def _create_issue_description(self, row: Dict, jira_key: str) -> str:
        """Create GitHub issue description from Jira data."""
        description_parts = []
        
        # Original Jira issue link
        jira_link = f"{self.JIRA_BROWSE_URL}{jira_key}"
        description_parts.append(f"**Original Jira Issue:** [{jira_key}]({jira_link})\n")
        
        # Convert description
        if row.get("Description"):
            description_parts.append("## Description\n")
            description_parts.append(
                self.jira_markup_to_markdown(row["Description"])
            )
            description_parts.append("\n")
        
        # Add custom fields — only Issue key, Issue Type, Status Category, and Custom field (*) columns
        custom_fields = []
        for key, value in row.items():
            if not value:
                continue
            k_lower = key.lower()
            if (
                k_lower == "issue key"
                or k_lower == "issue type"
                or k_lower == "status category"
                or k_lower.startswith("custom field")
            ):
                custom_fields.append(f"- **{key}:** {value}")
        
        if custom_fields:
            description_parts.append("## Custom Fields\n")
            description_parts.append("\n".join(custom_fields))
            description_parts.append("\n")
        
        # Add metadata
        metadata = []
        if row.get("Priority"):
            metadata.append(f"- **Priority:** {row['Priority']}")
        if row.get("Reporter"):
            metadata.append(f"- **Reporter:** {row['Reporter']}")
        if row.get("Assignee"):
            metadata.append(f"- **Assignee:** {row['Assignee']}")
        if row.get("Sprint"):
            metadata.append(f"- **Sprint:** {row['Sprint']}")
        
        if metadata:
            description_parts.append("## Metadata\n")
            description_parts.append("\n".join(metadata))
        
        return "\n".join(description_parts)
    
    def _get_issue_type(self, row: Dict) -> Optional[str]:
        """Get GitHub issue type name based on Jira type."""
        issue_type = row.get("Type", "").strip()
        return self.JIRA_TO_GITHUB_TYPE.get(issue_type) if issue_type else None
    
    def _create_or_update_issue(self, row: Dict) -> Tuple[int, bool]:
        """
        Create or update a GitHub issue.
        
        Returns:
            Tuple of (issue_number, created: bool)
        """
        jira_key = row.get("Key", "").strip()
        summary = row.get("Summary", "").strip()
        
        if not jira_key or not summary:
            return None, False
        
        # Check if issue already exists
        existing = self.existing_issues.get(summary)
        
        issue_type = self._get_issue_type(row)
        description = self._create_issue_description(row, jira_key)
        
        payload_base = {
            "title": summary,
            "body": description,
        }
        if issue_type:
            payload_base["type"] = issue_type

        if existing:
            # Update existing issue
            issue_number = existing["number"]
            url = (
                f"{self.github_api_base}/repos/{self.owner}/"
                f"{self.repo}/issues/{issue_number}"
            )
            
            try:
                response = self.session.patch(url, json=payload_base)
                response.raise_for_status()
                print(f"✓ Updated: {jira_key} -> Issue #{issue_number}")
                self.jira_issue_map[jira_key] = issue_number
                self.jira_issue_id_map[jira_key] = existing["id"]
                return issue_number, False
            except requests.exceptions.RequestException as e:
                body = getattr(e.response, 'text', '') if hasattr(e, 'response') and e.response is not None else ''
                print(f"✗ Failed to update {jira_key}: {e} | {body}")
                return None, False
        else:
            # Create new issue
            url = f"{self.github_api_base}/repos/{self.owner}/{self.repo}/issues"
            
            try:
                response = self.session.post(url, json=payload_base)
                response.raise_for_status()
                
                issue = response.json()
                issue_number = issue["number"]
                self.existing_issues[summary] = issue
                self.jira_issue_map[jira_key] = issue_number
                self.jira_issue_id_map[jira_key] = issue["id"]
                
                print(f"✓ Created: {jira_key} -> Issue #{issue_number}")
                return issue_number, True
            except requests.exceptions.RequestException as e:
                body = getattr(e.response, 'text', '') if hasattr(e, 'response') and e.response is not None else ''
                print(f"✗ Failed to create {jira_key}: {e} | {body}")
                return None, False
    
    def _collect_issue_links(self, row: Dict) -> Dict[str, List[str]]:
        """Collect linked issue keys from Jira link columns (e.g. 'Outward issue link (blocks)')."""
        links: Dict[str, List[str]] = {}
        for key, value in row.items():
            if value and re.search(r'issue link', key, re.IGNORECASE):
                jira_keys = re.findall(r'[A-Z]+-\d+', value)
                if jira_keys:
                    links[key] = jira_keys
        return links

    def _link_related_issues(self, issue_key: str, links: Dict[str, List[str]]):
        """Append related issue links to a GitHub issue body."""
        if issue_key not in self.jira_issue_map:
            return

        issue_number = self.jira_issue_map[issue_key]
        url = (
            f"{self.github_api_base}/repos/{self.owner}/"
            f"{self.repo}/issues/{issue_number}"
        )

        try:
            response = self.session.get(url)
            response.raise_for_status()
            body = response.json().get("body") or ""

            # Strip existing Related Issues section to regenerate it cleanly
            body = re.sub(r'\n\n## Related Issues\n.*$', '', body, flags=re.DOTALL)

            related_lines = []
            for link_type, keys in links.items():
                linked_nums = [
                    f"#{self.jira_issue_map[k]}"
                    for k in keys
                    if k in self.jira_issue_map
                ]
                if linked_nums:
                    related_lines.append(
                        f"- **{link_type}:** {', '.join(linked_nums)}"
                    )

            if related_lines:
                body += "\n\n## Related Issues\n" + "\n".join(related_lines)
                payload = {"body": body}
                response = self.session.patch(url, json=payload)
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Warning: Could not link related issues to {issue_key}: {e}")

    def _link_child_issues(self, parent_key: str, child_keys: List[str]):
        """Link child issues as native GitHub sub-issues using internal issue IDs."""
        if parent_key not in self.jira_issue_map:
            return

        parent_number = self.jira_issue_map[parent_key]
        sub_issues_url = (
            f"{self.github_api_base}/repos/{self.owner}/"
            f"{self.repo}/issues/{parent_number}/sub_issues"
        )
        headers = {**dict(self.session.headers), "Accept": "application/vnd.github+json"}

        for child_key in child_keys:
            if child_key not in self.jira_issue_id_map:
                continue
            child_id = self.jira_issue_id_map[child_key]
            try:
                response = self.session.post(
                    sub_issues_url,
                    json={"sub_issue_id": child_id},
                    headers=headers,
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 422:
                    pass  # already a sub-issue of this parent
                else:
                    err = getattr(e.response, 'text', str(e)) if hasattr(e, 'response') and e.response is not None else str(e)
                    print(f"Warning: Could not add sub-issue (id={child_id}) to #{parent_number}: {err}")
    
    def migrate(self, csv_file: str):
        """
        Migrate issues from Jira CSV to GitHub.
        
        Args:
            csv_file: Path to Jira CSV export
        """
        print(f"Starting migration from {csv_file}...\n")
        
        # First pass: create/update all issues
        parent_child_map = {}  # parent_key -> [child_keys]
        issue_links_map: Dict[str, Dict[str, List[str]]] = {}  # key -> {link_type -> [keys]}
        created = updated = failed = 0
        print(f"Found {len(self.existing_issues)} existing issue(s) in the repo (will update on title match).")

        try:
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = self._normalize_row(row)
                    jira_key = row.get("Key", "").strip()
                    parent_key = row.get("Parent", "").strip()
                    
                    # Track parent-child relationships
                    if parent_key:
                        if parent_key not in parent_child_map:
                            parent_child_map[parent_key] = []
                        parent_child_map[parent_key].append(jira_key)
                    
                    # Track other issue links
                    links = self._collect_issue_links(row)
                    if links:
                        issue_links_map[jira_key] = links
                    
                    # Create or update issue
                    issue_number, was_created = self._create_or_update_issue(row)
                    if issue_number is None:
                        failed += 1
                    elif was_created:
                        created += 1
                    else:
                        updated += 1
        except FileNotFoundError:
            print(f"Error: File {csv_file} not found")
            sys.exit(1)
        except Exception as e:
            print(f"Error reading CSV: {e}")
            sys.exit(1)
        
        # Second pass: link child issues to parents
        print(f"\nSummary: {created} created, {updated} updated, {failed} failed.")
        print(f"View: https://github.com/{self.owner}/{self.repo}/issues")
        print(f"\nLinking child issues ({len(parent_child_map)} parent(s) found)...")
        for parent_key, child_keys in parent_child_map.items():
            self._link_child_issues(parent_key, child_keys)
        
        # Third pass: link related issues
        print("\nLinking related issues...")
        for issue_key, links in issue_links_map.items():
            self._link_related_issues(issue_key, links)
        
        print("\n✓ Migration complete!")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate Jira issues from CSV to GitHub"
    )
    parser.add_argument("csv_file", help="Path to Jira CSV export")
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub personal access token (or use GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub repository owner",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository name",
    )
    
    args = parser.parse_args()
    
    if not args.token:
        print("Error: GitHub token required (use --token or GITHUB_TOKEN env var)")
        sys.exit(1)

    token_preview = f"{args.token[:8]}...{args.token[-4:]}"
    print(f"Using token: {token_preview}")

    migrator = JiraToGithubMigrator(args.token, args.owner, args.repo)
    migrator.migrate(args.csv_file)


if __name__ == "__main__":
    main()
