"""Read-only Jenkins REST client.

Every request must target a URL under the configured Jenkins base, uses
the service account's API token, never follows redirects, and must be a
GET: P1 performs no writes against Jenkins.
"""

from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from settings import get_settings
from utils.urls import url_under_base


class JenkinsError(Exception):
    pass


@dataclass
class BuildInfo:
    result: str | None
    parameters: dict[str, str]
    causes: list[str]


@dataclass
class ConsoleLog:
    text: str
    total_bytes: int
    truncated: bool


@dataclass
class Stage:
    name: str
    status: str


@dataclass
class BuildRef:
    number: int
    url: str
    result: str | None


def job_path(job: str) -> str:
    """'folder/name' -> 'job/folder/job/name/', encoded the way Jenkins does."""

    return "".join(
        f"job/{quote(part, safe='')}/" for part in job.split("/") if part
    )


def build_url_for(base_url: str, job: str, build_number: int) -> str:
    """Canonical build URL under our configured base, never caller-supplied."""

    return base_url.rstrip("/") + "/" + job_path(job) + f"{build_number}/"


def _is_success(status_code: int) -> bool:
    # Redirects count as failures: they usually mean an SSO/login page.
    return 200 <= status_code < 300


def _read_only_guard(request: httpx.Request) -> None:
    if request.method != "GET":
        raise JenkinsError(f"read-only client refused {request.method}")


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        api_token: str,
        *,
        timeout: float,
        max_log_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.max_log_bytes = max_log_bytes
        self._http = httpx.Client(
            auth=httpx.BasicAuth(username, api_token),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [_read_only_guard]},
        )

    def _check(self, url: str) -> None:
        if not url_under_base(url, self.base_url):
            raise JenkinsError("refusing URL outside Jenkins base")

    def _describe(self, url: str) -> str:
        # Path only: no host, query string or credentials in error text.
        return urlsplit(url).path

    def _get_json(self, url: str, params: dict | None = None):
        self._check(url)

        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as err:
            raise JenkinsError(
                f"{type(err).__name__} for {self._describe(url)}"
            ) from err

        if not _is_success(response.status_code):
            raise JenkinsError(
                f"HTTP {response.status_code} for {self._describe(url)}"
            )

        try:
            return response.json()
        except ValueError as err:
            raise JenkinsError(
                f"non-JSON response for {self._describe(url)}"
            ) from err

    def get_build_info(self, build_url: str) -> BuildInfo:
        data = self._get_json(
            build_url + "api/json",
            {
                "tree": "result,actions[parameters[name,value],"
                        "causes[shortDescription]]"
            },
        )

        parameters = {}
        causes = []

        for action in data.get("actions") or []:
            for param in action.get("parameters") or []:
                value = param.get("value")

                # Password parameters come back without a value.
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    parameters[param["name"]] = str(value)
                elif isinstance(value, bool):
                    parameters[param["name"]] = str(value).lower()

            for cause in action.get("causes") or []:
                if cause.get("shortDescription"):
                    causes.append(cause["shortDescription"])

        return BuildInfo(
            result=data.get("result"),
            parameters=parameters,
            causes=causes,
        )

    def get_console(self, build_url: str) -> ConsoleLog:
        url = build_url + "consoleText"
        self._check(url)

        buffer = bytearray()
        total = 0

        try:
            with self._http.stream("GET", url) as response:
                if not _is_success(response.status_code):
                    raise JenkinsError(
                        f"HTTP {response.status_code} for {self._describe(url)}"
                    )

                for chunk in response.iter_bytes():
                    total += len(chunk)
                    buffer.extend(chunk)

                    # Keep only the tail; memory stays bounded.
                    if len(buffer) > self.max_log_bytes:
                        del buffer[: len(buffer) - self.max_log_bytes]
        except httpx.HTTPError as err:
            raise JenkinsError(
                f"{type(err).__name__} for {self._describe(url)}"
            ) from err

        truncated = total > self.max_log_bytes
        text = buffer.decode("utf-8", errors="replace")

        if truncated and "\n" in text:
            # Drop the partial first line.
            text = text.split("\n", 1)[1]

        return ConsoleLog(text=text, total_bytes=total, truncated=truncated)

    def get_stages(self, build_url: str) -> list[Stage] | None:
        url = build_url + "wfapi/describe"
        self._check(url)

        try:
            response = self._http.get(url)
        except httpx.HTTPError as err:
            raise JenkinsError(
                f"{type(err).__name__} for {self._describe(url)}"
            ) from err

        # Pipeline Stage View plugin not installed, or not a pipeline job.
        if response.status_code == 404:
            return None

        if not _is_success(response.status_code):
            raise JenkinsError(
                f"HTTP {response.status_code} for {self._describe(url)}"
            )

        try:
            stages = response.json().get("stages") or []
        except ValueError as err:
            raise JenkinsError(
                f"non-JSON response for {self._describe(url)}"
            ) from err

        return [
            Stage(name=stage.get("name", ""), status=stage.get("status", ""))
            for stage in stages
        ]

    def job_url(self, job: str) -> str:
        return self.base_url + job_path(job)

    def list_builds(self, job: str, limit: int) -> list[BuildRef]:
        data = self._get_json(
            self.job_url(job) + "api/json",
            {"tree": f"builds[number,url,result]{{0,{limit}}}"},
        )

        return [
            BuildRef(
                number=build["number"],
                url=build["url"],
                result=build.get("result"),
            )
            for build in data.get("builds") or []
        ]


def jenkins_client_from_settings() -> JenkinsClient:
    settings = get_settings()

    if not (
        settings.jenkins_base_url
        and settings.jenkins_username
        and settings.jenkins_api_token
    ):
        raise JenkinsError("Jenkins is not configured")

    return JenkinsClient(
        settings.jenkins_base_url,
        settings.jenkins_username,
        settings.jenkins_api_token.get_secret_value(),
        timeout=settings.jenkins_timeout_seconds,
        max_log_bytes=settings.jenkins_max_log_bytes,
    )
