import urllib.parse
import httpx


class GitLabClient:
    def __init__(self, url: str, token: str, project_id: str):
        self._base = url.rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
        # Accept either numeric ID or path like "group/project"
        if "/" in str(project_id):
            self._project = urllib.parse.quote(str(project_id), safe="")
        else:
            self._project = str(project_id)

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v4/projects/{self._project}{path}"

    def create_branch(self, branch: str, ref: str = "main") -> dict:
        resp = httpx.post(
            self._url("/repository/branches"),
            headers=self._headers,
            json={"branch": branch, "ref": ref},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def commit_files(self, branch: str, message: str, files: list[dict]) -> dict:
        """Commit one or more files. Each file dict: {file_path, content, action}
        action: "create" | "update" | "delete" (default: "create")
        """
        resp = httpx.post(
            self._url("/repository/commits"),
            headers=self._headers,
            json={
                "branch": branch,
                "commit_message": message,
                "actions": [
                    {
                        "action": f.get("action", "create"),
                        "file_path": f["file_path"],
                        "content": f["content"],
                    }
                    for f in files
                ],
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def create_mr(self, source: str, target: str, title: str, description: str = "") -> dict:
        resp = httpx.post(
            self._url("/merge_requests"),
            headers=self._headers,
            json={
                "source_branch": source,
                "target_branch": target,
                "title": title,
                "description": description,
                "remove_source_branch": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def merge_mr(self, iid: int) -> dict:
        resp = httpx.put(
            self._url(f"/merge_requests/{iid}/merge"),
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_file(self, file_path: str, ref: str = "main") -> str | None:
        """Return file content as string, or None if the file doesn't exist."""
        resp = httpx.get(
            self._url(f"/repository/files/{urllib.parse.quote(file_path, safe='')}"),
            headers=self._headers,
            params={"ref": ref},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        import base64
        return base64.b64decode(resp.json()["content"]).decode()

    def get_mr(self, iid: int) -> dict:
        resp = httpx.get(
            self._url(f"/merge_requests/{iid}"),
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
