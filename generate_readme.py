#!/usr/bin/env python3
"""
Generate README.md listing open-source contributions from GitHub.
Discovers repositories with merged PRs or direct commits on default branch.
"""

import os
import sys
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_PR_DISPLAY = 3


def make_graphql_request(token: str, query: str, variables: Optional[Dict] = None) -> Dict:
    """Make a GraphQL request to GitHub API with rate limit handling."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        response = requests.post(GITHUB_GRAPHQL_URL, json=payload, headers=headers)
        
        # Check rate limit headers
        rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
        rate_limit_reset = response.headers.get("X-RateLimit-Reset")
        
        if rate_limit_remaining:
            remaining = int(rate_limit_remaining)
            if remaining < 10:
                if rate_limit_reset:
                    reset_time = int(rate_limit_reset)
                    wait_time = max(reset_time - int(datetime.now(timezone.utc).timestamp()), 0) + 1
                    if wait_time > 0:
                        print(f"Rate limit low ({remaining} remaining). Waiting {wait_time}s until reset...")
                        time.sleep(wait_time)
                        continue
                else:
                    # Fallback: exponential backoff
                    wait_time = retry_delay * (2 ** attempt)
                    print(f"Rate limit low ({remaining} remaining). Backing off for {wait_time}s...")
                    time.sleep(wait_time)
                    continue
        
        # Check for rate limit exceeded
        if response.status_code == 403:
            if rate_limit_reset:
                reset_time = int(rate_limit_reset)
                wait_time = max(reset_time - int(datetime.now(timezone.utc).timestamp()), 0) + 1
                print(f"Rate limit exceeded. Waiting {wait_time}s until reset...")
                time.sleep(wait_time)
                continue
            else:
                response.raise_for_status()
        
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            raise Exception(f"GraphQL errors: {data['errors']}")

        return data["data"]
    
    raise Exception("Max retries exceeded for GraphQL request")


def fetch_merged_prs(username: str, token: str) -> List[Dict]:
    """Fetch all merged PRs authored by the user."""
    all_prs = []
    cursor = None
    has_next_page = True

    query_template = """
    query($query: String!, $after: String) {
      search(query: $query, type: ISSUE, first: 100, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          ... on PullRequest {
            title
            url
            mergedAt
            isDraft
            repository {
              name
              url
              description
              isFork
              stargazerCount
              owner {
                login
              }
            }
          }
        }
      }
    }
    """

    while has_next_page:
        search_query = f"is:pr is:merged is:public author:{username}"
        variables = {"query": search_query}
        if cursor:
            variables["after"] = cursor

        data = make_graphql_request(token, query_template, variables)
        search_result = data["search"]
        page_info = search_result["pageInfo"]
        has_next_page = page_info["hasNextPage"]
        cursor = page_info["endCursor"]

        for node in search_result["nodes"]:
            if node and not node.get("isDraft", False):
                all_prs.append(node)

    return all_prs


def fetch_repositories_with_commits(username: str, token: str) -> Dict[str, Dict]:
    """Fetch repositories where user has direct commits on default branch.
    
    Note: commitContributionsByRepository is limited to 100 repos per query
    and only includes commits on default branch or gh-pages (per GitHub API).
    """
    repos_with_commits = {}

    # Query for repositories with commit contributions
    # Include occurredAt for sorting by most recent commit
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          commitContributionsByRepository(maxRepositories: 100) {
            repository {
              name
              url
              description
              isFork
              isPrivate
              stargazerCount
              defaultBranchRef {
                name
              }
              owner {
                login
              }
            }
            contributions(first: 1, orderBy: {field: OCCURRED_AT, direction: DESC}) {
              totalCount
              nodes {
                occurredAt
              }
            }
          }
        }
      }
    }
    """

    data = make_graphql_request(token, query, {"login": username})
    contributions = data["user"]["contributionsCollection"]
    commit_repos = contributions.get("commitContributionsByRepository", [])

    for repo_data in commit_repos:
        repo = repo_data["repository"]
        
        # Skip private repositories
        if repo.get("isPrivate"):
            continue
            
        repo_key = f"{repo['owner']['login']}/{repo['name']}"

        # Check if repo has a default branch (has commits there)
        # The contributionsCollection only includes repos with commits on default branch
        # We verify by checking defaultBranchRef exists and has contributions
        default_branch = repo.get("defaultBranchRef")
        contribution_count = repo_data["contributions"].get("totalCount", 0)
        
        # Get the most recent commit date for sorting
        latest_commit_date = None
        contribution_nodes = repo_data["contributions"].get("nodes", [])
        if contribution_nodes:
            latest_occurred = contribution_nodes[0].get("occurredAt")
            if latest_occurred:
                latest_commit_date = datetime.fromisoformat(latest_occurred.replace("Z", "+00:00"))
                if latest_commit_date.tzinfo:
                    latest_commit_date = latest_commit_date.replace(tzinfo=None)

        if default_branch and contribution_count > 0:
            repos_with_commits[repo_key] = {
                "repository": repo,
                "latest_commit_date": latest_commit_date,
            }

    return repos_with_commits


def filter_and_group_contributions(
    prs: List[Dict], repos_with_commits: Dict[str, Dict], username: str
) -> Dict[str, Dict]:
    """Filter and group contributions by repository."""
    repo_data: Dict[str, Dict] = defaultdict(lambda: {
        "name": "",
        "url": "",
        "description": "",
        "is_fork": False,
        "owner": "",
        "stargazer_count": 0,
        "prs": [],
        "has_commits": False,
        "latest_activity": None,
    })

    # Process PRs
    for pr in prs:
        repo = pr["repository"]
        repo_key = f"{repo['owner']['login']}/{repo['name']}"
        
        # Skip repos owned by the user
        if repo['owner']['login'] == username:
            continue
        
        # Skip repos with less than 1000 stars
        if repo.get("stargazerCount", 0) < 1000:
            continue

        repo_data[repo_key]["name"] = repo["name"]
        repo_data[repo_key]["url"] = repo["url"]
        repo_data[repo_key]["description"] = repo.get("description") or ""
        repo_data[repo_key]["is_fork"] = repo.get("isFork", False)
        repo_data[repo_key]["owner"] = repo['owner']['login']
        repo_data[repo_key]["stargazer_count"] = repo.get("stargazerCount", 0)
        repo_data[repo_key]["prs"].append({
            "title": pr["title"],
            "url": pr["url"],
            "merged_at": pr.get("mergedAt"),
        })

        # Update latest activity
        if pr.get("mergedAt"):
            merged_str = pr["mergedAt"]
            # Convert ISO format to naive datetime
            merged_date = datetime.fromisoformat(merged_str.replace("Z", "+00:00"))
            if merged_date.tzinfo:
                merged_date = merged_date.replace(tzinfo=None)
            
            current_latest = repo_data[repo_key]["latest_activity"]
            if current_latest is None or merged_date > current_latest:
                repo_data[repo_key]["latest_activity"] = merged_date

    # Process direct commits (only for repos not already covered by PRs)
    for repo_key, data in repos_with_commits.items():
        repo = data["repository"]
        
        # Skip repos owned by the user
        if repo['owner']['login'] == username:
            continue
        
        # Skip repos with less than 5000 stars
        if repo.get("stargazerCount", 0) < 5000:
            continue
        
        if repo_key not in repo_data:
            # New repo with only commits (no merged PRs)
            repo_data[repo_key]["name"] = repo["name"]
            repo_data[repo_key]["url"] = repo["url"]
            repo_data[repo_key]["description"] = repo.get("description") or ""
            repo_data[repo_key]["is_fork"] = repo.get("isFork", False)
            repo_data[repo_key]["owner"] = repo['owner']['login']
            repo_data[repo_key]["stargazer_count"] = repo.get("stargazerCount", 0)
            repo_data[repo_key]["has_commits"] = True
            # Use the latest commit date for sorting, or datetime.min if not available
            repo_data[repo_key]["latest_activity"] = data.get("latest_commit_date") or datetime.min
        else:
            # Repo already has PRs, but update latest_activity if commits are more recent
            latest_commit_date = data.get("latest_commit_date")
            if latest_commit_date:
                current_latest = repo_data[repo_key]["latest_activity"]
                if current_latest is None or latest_commit_date > current_latest:
                    repo_data[repo_key]["latest_activity"] = latest_commit_date
                repo_data[repo_key]["has_commits"] = True

    # Filter: exclude fork-only repos with no merged PRs
    # Note: commitContributionsByRepository already excludes forks, so forks with only commits won't appear
    filtered_repos = {}
    for repo_key, data in repo_data.items():
        if data["is_fork"] and not data["prs"]:
            continue
            
        # Sort PRs by merged_at descending
        if data["prs"]:
            data["prs"].sort(key=lambda x: x["merged_at"] or "", reverse=True)
            
        filtered_repos[repo_key] = data

    return filtered_repos


def generate_readme(repo_data: Dict[str, Dict], username: str) -> str:
    """Generate README.md markdown content."""
    # Sort by star count (most popular first)
    sorted_repos = sorted(
        repo_data.items(),
        key=lambda x: x[1]["stargazer_count"],
        reverse=True,
    )

    lines = [
        "## Open Source Contributions",
        ""
    ]

    if not sorted_repos:
        lines.append("No contributions found.")
        return "\n".join(lines)

    # Simple list format
    for repo_key, data in sorted_repos:
        owner = data["owner"]
        repo_name = data["name"]
        star_count = data["stargazer_count"]
        
        # Format star count (e.g., 33900 -> 33.9k)
        if star_count >= 1000:
            star_display = f"{star_count / 1000:.1f}k"
        else:
            star_display = str(star_count)
        
        # Build the PR search URL
        pr_search_url = f"https://github.com/{owner}/{repo_name}/pulls?q=is:pr+author:{username}+is:merged"
        
        lines.append(f"- **[{repo_name}]({pr_search_url})** ({star_display} ⭐)")

    return "\n".join(lines)


def main():
    """Main entry point."""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_TOKEN")

    if not username:
        print("Error: GITHUB_USERNAME environment variable not set", file=sys.stderr)
        sys.exit(1)

    if not token:
        print("Error: GITHUB_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching contributions for {username}...")
    
    # Fetch merged PRs
    print("Fetching merged PRs...")
    prs = fetch_merged_prs(username, token)
    print(f"Found {len(prs)} merged PRs")

    # Fetch repositories with direct commits
    print("Fetching repositories with direct commits...")
    repos_with_commits = fetch_repositories_with_commits(username, token)
    print(f"Found {len(repos_with_commits)} repositories with direct commits")

    # Filter and group
    print("Processing contributions...")
    repo_data = filter_and_group_contributions(prs, repos_with_commits, username)

    # Generate README
    print("Generating README.md...")
    contribution_content = generate_readme(repo_data, username)
    
    # Read existing README to preserve content above contributions
    header_content = "## Hi there 👋\n\nWelcome to my profile! Below you can find my open-source contributions."
    
    if os.path.exists("README.md"):
        with open("README.md", "r", encoding="utf-8") as f:
            content = f.read()
            if "<!-- START_CONTRIBUTIONS -->" in content:
                header_content = content.split("<!-- START_CONTRIBUTIONS -->")[0].strip()
            else:
                # If no markers, try to preserve everything before a potential existing header
                if "## Open Source Contributions" in content:
                    header_content = content.split("## Open Source Contributions")[0].strip()
                else:
                    header_content = content.strip()

    full_readme = f"{header_content}\n\n<!-- START_CONTRIBUTIONS -->\n{contribution_content}\n<!-- END_CONTRIBUTIONS -->\n"

    # Write to file
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(full_readme)

    print(f"Successfully generated README.md with {len(repo_data)} repositories")


if __name__ == "__main__":
    main()
