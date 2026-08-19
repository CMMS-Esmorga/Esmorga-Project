#!/usr/bin/env python3
"""Migrate Jira Cloud issues directly to GitHub Issues.

Attachments are copied into the destination repository and linked from their
GitHub issue because the public GitHub Issues API has no binary-upload endpoint.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth


def load_env_file(env_file: Path) -> None:
	"""Load simple KEY=VALUE pairs without overriding exported environment values."""
	if not env_file.is_file():
		return
	for line_number, line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), start=1):
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith("export "):
			line = line[7:].lstrip()
		if "=" not in line:
			raise ValueError(f"{env_file}:{line_number}: expected KEY=VALUE")
		key, value = line.split("=", maxsplit=1)
		key = key.strip()
		value = value.strip()
		if not key:
			raise ValueError(f"{env_file}:{line_number}: environment variable name is empty")
		if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
			value = value[1:-1]
		os.environ.setdefault(key, value)


class JiraToGitHubMigrator:
	"""Copy Jira Cloud issues, attachments, and relationships to GitHub."""

	JIRA_MARKER = "<!-- jira-migration-key: {key} -->"
	JIRA_MARKER_RE = re.compile(r"<!-- jira-migration-key: ([A-Z][A-Z0-9_]+-\d+) -->")
	JIRA_LINK_RE = re.compile(
		r"\*\*Original Jira Issue:\*\* \[([A-Z][A-Z0-9_]+-\d+)\]\([^)]*/browse/[A-Z][A-Z0-9_]+-\d+\)",
	)
	COMMENT_MARKER = "<!-- jira-migration-comment: {comment_id} -->"
	COMMENT_MARKER_RE = re.compile(r"<!-- jira-migration-comment: (\d+) -->")
	JIRA_TO_GITHUB_TYPE = {
		"Bug": "Bug",
		"Story": "Story",
		"Task": "Task",
		"Epic": "Feature",
		"Sub-task": "Task",
		"Subtask": "Task",
	}
	CLOSED_JIRA_STATUSES = {"done", "rejected"}
	OUT_OF_SCOPE_STATUS = "out of scope"
	OUT_OF_SCOPE_LABEL = "OutOfScope"
	CONTENTS_SIZE_LIMIT = 100 * 1024 * 1024
	MAX_GITHUB_WRITE_RETRIES = 5
	GITHUB_WRITE_INTERVAL_SECONDS = 1.0

	def __init__(
		self,
		jira_url: str,
		jira_email: str,
		jira_token: str,
		github_token: str,
		owner: str,
		repo: str,
		attachment_path: str = ".jira-attachments",
	):
		self.jira_url = jira_url.rstrip("/")
		self.owner = owner
		self.repo = repo
		self.attachment_path = attachment_path.strip("/")
		self.github_api = "https://api.github.com"
		self.github_web = f"https://github.com/{owner}/{repo}"
		self.default_branch = "main"
		self.issue_numbers: Dict[str, int] = {}
		self.issue_ids: Dict[str, int] = {}
		self.issue_numbers_by_title: Dict[str, int] = {}
		self.issue_ids_by_title: Dict[str, int] = {}
		self.ambiguous_titles = set()
		self.claimed_issue_numbers = set()
		self.epic_link_field_id: Optional[str] = None
		self.jira_field_names: Dict[str, str] = {}
		self.planned_issue_keys = set()
		self.dry_run = False
		self.report: Dict[str, Any] = {"updated": [], "skipped": [], "failed": []}
		self.next_github_write_at = 0.0

		self.jira = requests.Session()
		self.jira.trust_env = False
		self.jira.auth = HTTPBasicAuth(jira_email, jira_token)
		self.jira.headers.update({"Accept": "application/json"})

		self.github = requests.Session()
		self.github.trust_env = False
		self.github.headers.update(
			{
				"Accept": "application/vnd.github+json",
				"Authorization": f"Bearer {github_token}",
				"X-GitHub-Api-Version": "2022-11-28",
			}
		)

	@staticmethod
	def _log(message: str) -> None:
		print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)

	def verify_access(self) -> None:
		"""Verify credentials, cache the target branch, and resolve the Epic Link field."""
		try:
			jira_response = self.jira.get(f"{self.jira_url}/rest/api/3/myself")
			jira_response.raise_for_status()
			jira_name = jira_response.json().get("displayName", "<unknown>")
			self._log(f"Authenticated to Jira as: {jira_name}")

			github_response = self.github.get(f"{self.github_api}/repos/{self.owner}/{self.repo}")
			github_response.raise_for_status()
			repo_data = github_response.json()
			if not repo_data.get("has_issues", True):
				raise RuntimeError(f"Issues are disabled for {self.owner}/{self.repo}.")
			self.default_branch = repo_data.get("default_branch", "main")
		except requests.RequestException as error:
			raise RuntimeError(f"Could not authenticate to Jira or GitHub: {error}") from error
		self._resolve_epic_link_field()

	def _resolve_epic_link_field(self) -> None:
		"""Find the company-managed Jira field that records an issue's Epic."""
		try:
			response = self.jira.get(f"{self.jira_url}/rest/api/3/field")
			response.raise_for_status()
			for field in response.json():
				self.jira_field_names[field["id"]] = field.get("name") or field["id"]
				if field.get("name") == "Epic Link":
					self.epic_link_field_id = field["id"]
		except requests.RequestException as error:
			self._log(f"Warning: could not resolve Jira's Epic Link field: {error}")

	@staticmethod
	def _jira_markup_to_markdown(text: str) -> str:
		"""Convert legacy Jira wiki markup when the API returns a plain string."""
		text = re.sub(
			r"^(\*+)\s+(.+)$",
			lambda match: "  " * (len(match.group(1)) - 1) + "- " + match.group(2),
			text,
			flags=re.MULTILINE,
		)
		text = re.sub(
			r"^(#+)\s+(.+)$",
			lambda match: "  " * (len(match.group(1)) - 1) + "1. " + match.group(2),
			text,
			flags=re.MULTILINE,
		)
		text = re.sub(r"h([1-6])\.\s+", lambda match: "#" * int(match.group(1)) + " ", text)
		text = re.sub(r"\{code(?::[\w]+)?\}(.*?)\{code\}", lambda match: f"```\n{match.group(1)}\n```", text, flags=re.DOTALL)
		text = re.sub(r"\[([^\]|]+)\|([^\]]+)\]", r"[\1](\2)", text)
		text = re.sub(r"\{\{([^}]+)\}\}", r"`\1`", text)
		text = re.sub(r"\*([^*]+)\*", r"**\1**", text)
		text = re.sub(r"_([^_]+)_", r"*\1*", text)
		return text.strip()

	@staticmethod
	def _adf_to_markdown(node: Any) -> str:
		"""Render the common Jira Atlassian Document Format nodes as Markdown."""
		if isinstance(node, str):
			return JiraToGitHubMigrator._jira_markup_to_markdown(node)
		if not isinstance(node, dict):
			return ""

		node_type = node.get("type", "")
		content = "".join(JiraToGitHubMigrator._adf_to_markdown(child) for child in node.get("content", []))
		if node_type == "text":
			text = node.get("text", "")
			for mark in node.get("marks", []):
				mark_type = mark.get("type")
				if mark_type == "strong":
					text = f"**{text}**"
				elif mark_type == "em":
					text = f"*{text}*"
				elif mark_type == "code":
					text = f"`{text}`"
				elif mark_type == "link":
					href = mark.get("attrs", {}).get("href", "")
					text = f"[{text}]({href})" if href else text
			return text
		if node_type == "hardBreak":
			return "\n"
		if node_type == "paragraph":
			return f"{content}\n\n"
		if node_type == "heading":
			level = node.get("attrs", {}).get("level", 2)
			return f"{'#' * level} {content}\n\n"
		if node_type == "bulletList":
			return content + "\n"
		if node_type == "orderedList":
			return content + "\n"
		if node_type == "listItem":
			return f"- {content.strip()}\n"
		if node_type == "codeBlock":
			language = node.get("attrs", {}).get("language") or ""
			return f"```{language}\n{content.rstrip()}\n```\n\n"
		if node_type == "blockquote":
			return "".join(f"> {line}\n" for line in content.strip().splitlines()) + "\n"
		if node_type == "rule":
			return "---\n\n"
		if node_type == "mention":
			return node.get("attrs", {}).get("text", "")
		return content

	def _search_issues(self, jql: str) -> Iterable[Dict[str, Any]]:
		"""Yield Jira issues, preferring the current cursor-based search endpoint."""
		fields = ["*all"]
		url = f"{self.jira_url}/rest/api/3/search/jql"
		params: Dict[str, Any] = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
		response = self.jira.get(url, params=params)
		if response.status_code == 404:
			yield from self._search_issues_legacy(jql, fields)
			return

		response.raise_for_status()
		while True:
			data = response.json()
			yield from data.get("issues", [])
			next_page = data.get("nextPageToken")
			if not next_page:
				return
			params["nextPageToken"] = next_page
			response = self.jira.get(url, params=params)
			response.raise_for_status()

	def _search_issues_legacy(self, jql: str, fields: List[str]) -> Iterable[Dict[str, Any]]:
		"""Support Jira Cloud sites that still expose the offset-based endpoint."""
		url = f"{self.jira_url}/rest/api/3/search"
		start_at = 0
		while True:
			response = self.jira.get(
				url,
				params={"jql": jql, "fields": ",".join(fields), "maxResults": 100, "startAt": start_at},
			)
			response.raise_for_status()
			data = response.json()
			issues = data.get("issues", [])
			yield from issues
			start_at += len(issues)
			if not issues or start_at >= data.get("total", 0):
				return

	def _fetch_existing_issues(self) -> None:
		"""Map existing GitHub issues by Jira source reference and exact title."""
		self._log("Loading existing GitHub issues for matching...")
		page = 1
		while True:
			response = self.github.get(
				f"{self.github_api}/repos/{self.owner}/{self.repo}/issues",
				params={"state": "all", "per_page": 100, "page": page},
			)
			response.raise_for_status()
			issues = response.json()
			if not issues:
				self._log(
					f"Loaded {len(self.issue_numbers)} Jira-link match(es); "
					f"{len(self.ambiguous_titles)} duplicate title(s) will be skipped."
				)
				return
			for issue in issues:
				if "pull_request" in issue:
					continue
				title = issue["title"]
				if title in self.issue_numbers_by_title:
					self.ambiguous_titles.add(title)
				else:
					self.issue_numbers_by_title[title] = issue["number"]
					self.issue_ids_by_title[title] = issue["id"]
				body = issue.get("body") or ""
				match = self.JIRA_MARKER_RE.search(body) or self.JIRA_LINK_RE.search(body)
				if match:
					key = match.group(1)
					self.issue_numbers[key] = issue["number"]
					self.issue_ids[key] = issue["id"]
					self.claimed_issue_numbers.add(issue["number"])
			page += 1

	@staticmethod
	def _person_name(person: Optional[Dict[str, Any]]) -> str:
		return (person or {}).get("displayName") or ""

	def _custom_field_value(self, value: Any) -> str:
		"""Format Jira custom field values without leaking API implementation details."""
		if isinstance(value, str):
			return self._adf_to_markdown(value)
		if isinstance(value, list):
			return ", ".join(filter(None, (self._custom_field_value(item) for item in value)))
		if isinstance(value, dict):
			for key in ("displayName", "name", "value", "key", "summary"):
				if value.get(key):
					return self._custom_field_value(value[key])
			return self._adf_to_markdown(value) if value.get("type") else ""
		return str(value) if value is not None else ""

	def _attachment_markdown(self, filename: str, blob_url: str) -> str:
		"""Embed image attachments while retaining ordinary files as download links."""
		image_extensions = {".apng", ".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
		if PurePosixPath(filename).suffix.lower() in image_extensions:
			raw_url = blob_url.replace(
				f"https://github.com/{self.owner}/{self.repo}/blob/",
				f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/",
			)
			return f"![{filename}]({raw_url})"
		return f"- [{filename}]({blob_url})"

	def _issue_body(self, issue: Dict[str, Any], attachment_links: List[Tuple[str, str]]) -> str:
		"""Create the repeatable, source-data portion of a GitHub issue body."""
		fields = issue["fields"]
		key = issue["key"]
		body = [
			self.JIRA_MARKER.format(key=key),
			f"**Original Jira Issue:** [{key}]({self.jira_url}/browse/{key})",
		]
		description = self._adf_to_markdown(fields.get("description")).strip()
		if description:
			body.extend(["", "## Description", "", description])

		metadata = []
		for label, value in (
			("Type", (fields.get("issuetype") or {}).get("name")),
			("Status", (fields.get("status") or {}).get("name")),
			("Original Jira created", fields.get("created")),
			("Priority", (fields.get("priority") or {}).get("name")),
			("Reporter", self._person_name(fields.get("reporter"))),
			("Assignee", self._person_name(fields.get("assignee"))),
		):
			if value:
				metadata.append(f"- **{label}:** {value}")
		labels = fields.get("labels") or []
		if labels:
			metadata.append(f"- **Labels:** {', '.join(labels)}")
		if metadata:
			body.extend(["", "## Metadata", "", *metadata])

		if attachment_links:
			body.extend(["", "## Attachments", ""])
			body.extend(self._attachment_markdown(name, url) for name, url in attachment_links)
		return "\n".join(body)

	def _content_path(self, issue_key: str, filename: str, attachment_id: Optional[str]) -> str:
		"""Produce a safe, stable repository path for a Jira attachment."""
		safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", PurePosixPath(filename).name)
		prefix = f"{attachment_id}-" if attachment_id else ""
		return f"{self.attachment_path}/{issue_key}/{prefix}{safe_name or 'attachment'}"

	def _upload_attachment(self, issue_key: str, attachment: Dict[str, Any]) -> Optional[Tuple[str, str]]:
		"""Download one Jira attachment and store it in the GitHub repository."""
		filename = attachment.get("filename") or "attachment"
		download_url = attachment.get("content")
		if not download_url:
			self._log(f"Warning: {issue_key}: attachment {filename!r} has no download URL.")
			return None
		try:
			source = self.jira.get(download_url)
			source.raise_for_status()
			content = source.content
			if len(content) > self.CONTENTS_SIZE_LIMIT:
				self._log(f"Warning: {issue_key}: skipped {filename!r}; GitHub Contents API limit is 100 MiB.")
				return None

			path = self._content_path(issue_key, filename, str(attachment.get("id") or ""))
			api_path = quote(path, safe="/")
			target_url = f"{self.github_api}/repos/{self.owner}/{self.repo}/contents/{api_path}"
			existing = self._github_read(target_url, params={"ref": self.default_branch})
			payload: Dict[str, str] = {
				"message": f"Migrate Jira attachment {issue_key}: {filename}",
				"content": base64.b64encode(content).decode("ascii"),
				"branch": self.default_branch,
			}
			if existing.status_code == 200:
				link = f"{self.github_web}/blob/{quote(self.default_branch, safe='')}/{api_path}"
				self._log(f"  {issue_key}: attachment already present: {filename}")
				return filename, link
			elif existing.status_code != 404:
				existing.raise_for_status()
			uploaded = self._github_write("PUT", target_url, json=payload)
			uploaded.raise_for_status()
			link = f"{self.github_web}/blob/{quote(self.default_branch, safe='')}/{api_path}"
			return filename, link
		except requests.RequestException as error:
			self._log(f"Warning: {issue_key}: could not migrate attachment {filename!r}: {error}")
			return None

	def _migrate_attachments(self, issue: Dict[str, Any]) -> List[Tuple[str, str]]:
		key = issue["key"]
		links = []
		for attachment in issue["fields"].get("attachment") or []:
			result = self._upload_attachment(key, attachment)
			if result:
				links.append(result)
		return links

	def _plan_attachments(self, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
		"""Inspect destination attachment paths without downloading or changing files."""
		plans = []
		for attachment in issue["fields"].get("attachment") or []:
			filename = attachment.get("filename") or "attachment"
			path = self._content_path(issue["key"], filename, str(attachment.get("id") or ""))
			api_path = quote(path, safe="/")
			response = self._github_read(
				f"{self.github_api}/repos/{self.owner}/{self.repo}/contents/{api_path}",
				params={"ref": self.default_branch},
			)
			if response.status_code not in (200, 404):
				response.raise_for_status()
			plans.append({
				"filename": filename,
				"size": attachment.get("size"),
				"destination": path,
				"operation": "reuse" if response.status_code == 200 else "upload",
			})
		return plans

	def _match_issue(self, issue: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[str]]:
		"""Return the matched GitHub issue and matching method, without changing state."""
		key = issue["key"]
		summary = issue["fields"].get("summary") or key
		if key in self.issue_numbers:
			return self.issue_numbers[key], self.issue_ids[key], "jira_key"
		if summary not in self.ambiguous_titles and summary in self.issue_numbers_by_title:
			number = self.issue_numbers_by_title[summary]
			if number not in self.claimed_issue_numbers:
				return number, self.issue_ids_by_title.get(summary), "title"
		return None, None, None

	@staticmethod
	def _github_error_details(error: requests.RequestException) -> str:
		"""Expose GitHub's refusal reason, especially primary and secondary rate limits."""
		response = getattr(error, "response", None)
		if response is None:
			return str(error)
		details = response.text.strip()
		rate_limit = response.headers.get("X-RateLimit-Remaining")
		retry_after = response.headers.get("Retry-After")
		parts = [f"HTTP {response.status_code}", details]
		if rate_limit is not None:
			parts.append(f"rate-limit remaining={rate_limit}")
		if retry_after:
			parts.append(f"retry after {retry_after}s")
		return " | ".join(part for part in parts if part)

	def _github_write(self, method: str, url: str, **kwargs: Any) -> requests.Response:
		"""Pace writes and retry GitHub's temporary secondary-rate-limit responses."""
		for attempt in range(1, self.MAX_GITHUB_WRITE_RETRIES + 1):
			wait_seconds = self.next_github_write_at - time.monotonic()
			if wait_seconds > 0:
				time.sleep(wait_seconds)
			request = getattr(self.github, method.lower())
			response = request(url, **kwargs)
			self.next_github_write_at = time.monotonic() + self.GITHUB_WRITE_INTERVAL_SECONDS
			if response.status_code not in (403, 429):
				return response

			message = response.text.lower()
			if "secondary rate limit" not in message and response.status_code != 429:
				return response
			if attempt == self.MAX_GITHUB_WRITE_RETRIES:
				return response
			retry_after = response.headers.get("Retry-After")
			backoff_seconds = float(retry_after) if retry_after else 60.0 * attempt
			self._log(
				f"GitHub secondary rate limit; retrying write in {backoff_seconds:.0f}s "
				f"({attempt}/{self.MAX_GITHUB_WRITE_RETRIES - 1})."
			)
			time.sleep(backoff_seconds)
		return response

	def _github_read(self, url: str, **kwargs: Any) -> requests.Response:
		"""Retry read requests when GitHub temporarily applies a secondary limit."""
		for attempt in range(1, self.MAX_GITHUB_WRITE_RETRIES + 1):
			response = self.github.get(url, **kwargs)
			if response.status_code not in (403, 429):
				return response
			message = response.text.lower()
			if "secondary rate limit" not in message and response.status_code != 429:
				return response
			if attempt == self.MAX_GITHUB_WRITE_RETRIES:
				return response
			retry_after = response.headers.get("Retry-After")
			backoff_seconds = float(retry_after) if retry_after else 60.0 * attempt
			self._log(
				f"GitHub secondary rate limit; retrying read in {backoff_seconds:.0f}s "
				f"({attempt}/{self.MAX_GITHUB_WRITE_RETRIES - 1})."
			)
			time.sleep(backoff_seconds)
		return response

	def _create_or_update_issue(self, issue: Dict[str, Any]) -> bool:
		"""Update only a pre-existing GitHub issue matched by Jira key or exact title."""
		key = issue["key"]
		summary = issue["fields"].get("summary") or key
		number, issue_id, matched_by = self._match_issue(issue)
		if not number:
			matched_by = "new_issue"
		attachment_plan = self._plan_attachments(issue) if self.dry_run else None
		attachments = [] if self.dry_run else self._migrate_attachments(issue)
		payload: Dict[str, Any] = {"title": summary, "body": self._issue_body(issue, attachments)}
		custom_fields = []
		for field_id, value in issue["fields"].items():
			if not field_id.startswith("customfield_") or value in (None, "", [], {}):
				continue
			field_name = self.jira_field_names.get(field_id, field_id)
			formatted_value = self._custom_field_value(value).strip()
			if formatted_value:
				custom_fields.append(f"- **{field_name}:** {formatted_value}")
		if custom_fields:
			payload["body"] += "\n\n## Custom Fields\n\n" + "\n".join(custom_fields)
		jira_type = (issue["fields"].get("issuetype") or {}).get("name")
		github_type = self.JIRA_TO_GITHUB_TYPE.get(jira_type)
		if github_type:
			payload["type"] = github_type
		status = issue["fields"].get("status") or {}
		status_name = (status.get("name") or "").casefold()
		status_category = (status.get("statusCategory") or {}).get("key", "").casefold()
		if status_name in self.CLOSED_JIRA_STATUSES or status_category == "done":
			payload["state"] = "closed"
		if status_name == self.OUT_OF_SCOPE_STATUS:
			payload["state"] = "closed"
			payload["labels"] = [self.OUT_OF_SCOPE_LABEL]
		if self.dry_run:
			if number:
				self.issue_numbers[key] = number
				self.issue_ids[key] = issue_id
				self.claimed_issue_numbers.add(number)
			self.report.setdefault("planned_issue_updates", []).append({
				"key": key,
				"issue_number": number,
				"matched_by": matched_by,
				"operation": "create" if not number else "update",
				"set_type": github_type,
				"set_state": payload.get("state", "open"),
				"set_labels": payload.get("labels", []),
				"custom_fields": len(custom_fields),
				"attachments": attachment_plan,
			})
			self._log(f"Plan {key}: {'create' if not number else f'update #{number}'}, {len(attachment_plan)} attachment operation(s).")
			return True
		try:
			if number:
				response = self._github_write(
					"PATCH", f"{self.github_api}/repos/{self.owner}/{self.repo}/issues/{number}", json=payload
				)
			else:
				response = self._github_write(
					"POST", f"{self.github_api}/repos/{self.owner}/{self.repo}/issues", json=payload
				)
			response.raise_for_status()
			result = response.json()
			self.issue_numbers[key] = result["number"]
			self.issue_ids[key] = result["id"]
			self.claimed_issue_numbers.add(result["number"])
			action = "Updated" if number else "Created"
			self._log(f"{action} {key} -> #{result['number']}")
			self.report["updated"].append({"key": key, "issue_number": result["number"], "operation": action.lower()})
			return True
		except requests.RequestException as error:
			details = self._github_error_details(error)
			self._log(f"Error: {key}: could not create or update issue: {details}")
			self.report["failed"].append({"key": key, "error": details})
			return False

	def _fetch_jira_comments(self, issue_key: str) -> Iterable[Dict[str, Any]]:
		"""Yield every comment, including comments beyond Jira's default page size."""
		url = f"{self.jira_url}/rest/api/3/issue/{issue_key}/comment"
		start_at = 0
		while True:
			response = self.jira.get(url, params={"startAt": start_at, "maxResults": 100})
			response.raise_for_status()
			data = response.json()
			comments = data.get("comments", [])
			yield from comments
			start_at += len(comments)
			if not comments or start_at >= data.get("total", 0):
				return

	def _migrate_comments(self, issue: Dict[str, Any]) -> None:
		"""Copy Jira comments once, preserving their author and creation time in Markdown."""
		key = issue["key"]
		number = self.issue_numbers.get(key)
		if not number:
			return
		try:
			comments_url = f"{self.github_api}/repos/{self.owner}/{self.repo}/issues/{number}/comments"
			existing_markers = set()
			page = 1
			while True:
				response = self._github_read(comments_url, params={"per_page": 100, "page": page})
				response.raise_for_status()
				existing_comments = response.json()
				if not existing_comments:
					break
				for comment in existing_comments:
					existing_markers.update(self.COMMENT_MARKER_RE.findall(comment.get("body") or ""))
				page += 1

			for comment in self._fetch_jira_comments(key):
				comment_id = str(comment["id"])
				if comment_id in existing_markers:
					continue
				if self.dry_run:
					self.report.setdefault("planned_comment_imports", []).append({"key": key, "comment_id": comment_id})
					continue
				author = self._person_name(comment.get("author")) or "Unknown Jira user"
				created = comment.get("created") or "unknown date"
				content = self._adf_to_markdown(comment.get("body")).strip() or "_(empty Jira comment)_"
				body = "\n".join([
					self.COMMENT_MARKER.format(comment_id=comment_id),
					f"**Migrated from Jira** by {author} on {created}",
					"",
					content,
				])
				response = self._github_write("POST", comments_url, json={"body": body})
				response.raise_for_status()
		except requests.RequestException as error:
			self._log(f"Warning: {key}: could not migrate comments: {self._github_error_details(error)}")

	@staticmethod
	def _jira_links(issue: Dict[str, Any]) -> List[Tuple[str, str]]:
		"""Return (Jira relationship label, target issue key) pairs."""
		links = []
		for link in issue["fields"].get("issuelinks") or []:
			link_type = link.get("type") or {}
			if "outwardIssue" in link:
				target = link["outwardIssue"].get("key")
				label = link_type.get("outward") or link_type.get("name") or "relates to"
			else:
				target = (link.get("inwardIssue") or {}).get("key")
				label = link_type.get("inward") or link_type.get("name") or "related to"
			if target:
				links.append((label, target))
		return links

	@staticmethod
	def _github_relationship_type(jira_relationship: str) -> str:
		"""Map Jira's relationship vocabulary to GitHub-supported relationship types."""
		relationship = jira_relationship.lower()
		if "is blocked by" in relationship or "blocked by" in relationship:
			return "blocked_by"
		if "blocks" in relationship:
			return "blocks"
		return "relates_to"

	def _link_related_issues(self, issue: Dict[str, Any]) -> None:
		"""Preserve non-parent Jira issue links in a generated body section."""
		key = issue["key"]
		number = self.issue_numbers.get(key)
		if not number:
			return
		related = [
			(self._github_relationship_type(label), target)
			for label, target in self._jira_links(issue)
			if target in self.issue_numbers or (self.dry_run and target in self.planned_issue_keys)
		]
		if not related:
			return
		if self.dry_run:
			self.report.setdefault("planned_related_issue_links", []).extend({
				"key": key,
				"issue_number": number,
				"relationship": label,
				"target_key": target,
				"target_issue_number": self.issue_numbers.get(target),
				"native_operation": "dependency" if label != "relates_to" else "relates_to_section",
			} for label, target in related)
			return
		try:
			url = f"{self.github_api}/repos/{self.owner}/{self.repo}/issues/{number}"
			response = self.github.get(url)
			response.raise_for_status()
			body = response.json().get("body") or ""
			body = re.sub(r"\n\n## Related Issues\n.*$", "", body, flags=re.DOTALL)
			lines = [f"- **{label.replace('_', ' ')}:** #{self.issue_numbers[target]}" for label, target in related]
			response = self._github_write(
				"PATCH", url, json={"body": body + "\n\n## Related Issues\n" + "\n".join(lines)}
			)
			response.raise_for_status()
			for relationship, target in related:
				if relationship == "relates_to":
					continue
				if relationship == "blocks":
					dependency_issue_number = self.issue_numbers[target]
					dependency_issue_id = self.issue_ids[key]
				else:
					dependency_issue_number = number
					dependency_issue_id = self.issue_ids[target]
				dependency = self._github_write(
					"POST",
					f"{self.github_api}/repos/{self.owner}/{self.repo}/issues/"
					f"{dependency_issue_number}/dependencies/blocked_by",
					json={"issue_id": dependency_issue_id},
				)
				if dependency.status_code != 422:
					dependency.raise_for_status()
		except requests.RequestException as error:
			print(f"Warning: {key}: could not add related-issue links: {error}")

	def _parent_key(self, issue: Dict[str, Any]) -> Optional[str]:
		"""Return a Jira parent key from the modern parent field or legacy Epic Link."""
		fields = issue["fields"]
		parent = fields.get("parent") or {}
		if parent.get("key"):
			return parent["key"]
		if not self.epic_link_field_id:
			return None
		epic_link = fields.get(self.epic_link_field_id)
		if isinstance(epic_link, dict):
			return epic_link.get("key")
		return epic_link if isinstance(epic_link, str) else None

	def _link_parent(self, issue: Dict[str, Any]) -> None:
		"""Create native GitHub sub-issues for Jira parent and Epic Link relationships."""
		key = issue["key"]
		parent_key = self._parent_key(issue)
		if not parent_key:
			return
		if self.dry_run:
			if parent_key not in self.planned_issue_keys:
				return
			self.report.setdefault("planned_sub_issue_links", []).append({
				"parent_key": parent_key, "parent_issue_number": self.issue_numbers.get(parent_key),
				"child_key": key, "child_issue_number": self.issue_numbers.get(key),
			})
			return
		if parent_key not in self.issue_numbers or key not in self.issue_ids:
			return
		parent_number = self.issue_numbers[parent_key]
		try:
			response = self._github_write(
				"POST",
				f"{self.github_api}/repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
				json={"sub_issue_id": self.issue_ids[key]},
			)
			if response.status_code != 422:  # Existing sub-issue links return validation errors.
				response.raise_for_status()
		except requests.RequestException as error:
			print(f"Warning: {key}: could not add parent relationship to {parent_key}: {error}")

	def migrate(self, jql: str, dry_run: bool = False) -> None:
		"""Run all migration passes for the JQL result set."""
		self.dry_run = dry_run
		self.report["mode"] = "dry_run" if dry_run else "apply"
		self._log("Starting Jira-to-GitHub dry run; GitHub will not be modified." if dry_run else "Starting Jira-to-GitHub migration.")
		self.verify_access()
		self._fetch_existing_issues()
		self._log("Loading Jira issues...")
		issues = list(self._search_issues(jql))
		self.planned_issue_keys = {issue["key"] for issue in issues}
		self._log(f"Found {len(issues)} Jira issue(s); {'planning' if dry_run else 'beginning'} issue updates.")

		succeeded = []
		for index, issue in enumerate(issues, start=1):
			self._log(f"Issue {index}/{len(issues)}: {issue['key']}")
			if self._create_or_update_issue(issue):
				succeeded.append(issue)
		self._log(f"{'Planning' if dry_run else 'Migrating'} comments for {len(succeeded)} updated issue(s)...")
		for issue in succeeded:
			self._migrate_comments(issue)
		self._log(f"{'Planning' if dry_run else 'Linking'} parent and Epic relationships...")
		for issue in succeeded:
			self._link_parent(issue)
		self._log(f"{'Planning' if dry_run else 'Linking'} other Jira issue relationships...")
		for issue in succeeded:
			self._link_related_issues(issue)
		self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
		self.report["jira_issues_found"] = len(issues)
		report_path = Path(__file__).with_name(
			"jira-migration-dry-run-report.json" if dry_run else "jira-migration-report.json"
		)
		report_path.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
		self._log(
			f"{'Dry run complete' if dry_run else 'Migration complete'}: {len(succeeded)} "
			f"{'planned' if dry_run else 'updated'}, {len(self.report['skipped'])} skipped, "
			f"{len(self.report['failed'])} failed. Report: {report_path.name}"
		)


def main() -> None:
	load_env_file(Path(__file__).with_name(".env"))
	required = [
		"JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_JQL",
		"GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO",
	]
	missing = [name for name in required if not os.getenv(name)]
	if missing:
		print("Error: missing required settings in .env: " + ", ".join(missing), file=sys.stderr)
		print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
		sys.exit(2)
	dry_run = os.getenv("DRY_RUN", "YES").upper() == "YES"
	if not dry_run and os.getenv("MIGRATION_CONFIRM") != "YES":
		print("Error: set MIGRATION_CONFIRM=YES in .env to permit GitHub updates.", file=sys.stderr)
		sys.exit(2)

	migrator = JiraToGitHubMigrator(
		os.environ["JIRA_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"],
		os.environ["GITHUB_TOKEN"], os.environ["GITHUB_OWNER"], os.environ["GITHUB_REPO"],
		os.getenv("GITHUB_ATTACHMENT_PATH", ".jira-attachments"),
	)
	try:
		migrator.migrate(os.environ["JIRA_JQL"], dry_run=dry_run)
	except RuntimeError as error:
		print(f"Error: {error}", file=sys.stderr)
		sys.exit(1)


if __name__ == "__main__":
	main()
