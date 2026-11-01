"""Read-only Jenkins REST client.

Every request must target a URL under the configured Jenkins base, uses
the service account's API token and never follows redirects. By default the
client is GET-only. A client built with allow_writes=True may additionally
POST to a job's `build` / `buildWithParameters` endpoint, and nothing else:
that is the only write the remediation executor needs.
"""

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

import httpx

from settings import get_settings
from utils.urls import url_under_base


class JenkinsError(Exception):
    pass


class JenkinsUncertainError(JenkinsError):
    """A write may or may not have been applied (transport failure after the
    request left). The caller must not retry it blindly."""


# Injectable so tests need not really wait.
_sleep = time.sleep


@dataclass
class BuildInfo:
    result: str | None
    parameters: dict[str, str]
    causes: list[str]
    # Parameters Jenkins reported without a usable value (password/secret
    # parameters): a rebuild could not reproduce them.
    masked_parameters: list[str] = field(default_factory=list)


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
    # Epoch milliseconds, as Jenkins reports it; None if absent.
    timestamp: int | None = None


@dataclass
class JobRef:
    """A job and its latest completed build, from one Jenkins tree query."""

    full_name: str
    disabled: bool
    last_number: int | None
    last_result: str | None
    # Epoch milliseconds of the latest completed build.
    last_timestamp: int | None = None


# Folder nesting walked by list_jobs (root + this many folder levels).
_JOB_TREE_DEPTH = 4
_JOB_FIELDS = (
    "fullName,color,"
    "lastCompletedBuild[number,result,timestamp]"
)


def _jobs_tree(depth: int) -> str:
    inner = f"jobs[{_JOB_FIELDS}{{nested}}]"
    tree = ""

    for _ in range(depth):
        tree = inner.replace("{nested}", f",{tree}" if tree else "")

    return tree


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


_REBUILD_PATH = re.compile(r"^/(?:job/[^/]+/)+(?:build|buildWithParameters)$")
_QUEUE_PATH = re.compile(r"^/queue/item/\d+/$")


def _write_guard(request: httpx.Request) -> None:
    if request.method == "GET":
        return

    if request.method == "POST" and _REBUILD_PATH.match(request.url.path):
        return

    raise JenkinsError(
        f"client refused {request.method} {urlsplit(str(request.url)).path}"
    )


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
        allow_writes: bool = False,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.max_log_bytes = max_log_bytes
        self.allow_writes = allow_writes
        self._http = httpx.Client(
            auth=httpx.BasicAuth(username, api_token),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            event_hooks={
                "request": [_write_guard if allow_writes else _read_only_guard]
            },
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
        masked = []
        causes = []

        for action in data.get("actions") or []:
            for param in action.get("parameters") or []:
                value = param.get("value")

                # Password parameters come back without a value.
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    parameters[param["name"]] = str(value)
                elif isinstance(value, bool):
                    parameters[param["name"]] = str(value).lower()
                elif param.get("name"):
                    masked.append(param["name"])

            for cause in action.get("causes") or []:
                if cause.get("shortDescription"):
                    causes.append(cause["shortDescription"])

        return BuildInfo(
            result=data.get("result"),
            parameters=parameters,
            causes=causes,
            masked_parameters=masked,
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

    def close(self) -> None:
        self._http.close()

    def job_url(self, job: str) -> str:
        return self.base_url + job_path(job)

    def list_builds(self, job: str, limit: int) -> list[BuildRef]:
        data = self._get_json(
            self.job_url(job) + "api/json",
            {"tree": f"builds[number,url,result,timestamp]{{0,{limit}}}"},
        )

        return [
            BuildRef(
                number=build["number"],
                url=build["url"],
                result=build.get("result"),
                timestamp=build.get("timestamp"),
            )
            for build in data.get("builds") or []
        ]


    def get_config_xml(self, job: str, max_bytes: int = 1_000_000) -> str:
        """The job's config.xml (read-only GET). Can contain secrets: callers
        must extract what they need and never store or log the document."""

        url = self.job_url(job) + "config.xml"
        self._check(url)

        try:
            response = self._http.get(url)
        except httpx.HTTPError as err:
            raise JenkinsError(
                f"{type(err).__name__} for {self._describe(url)}"
            ) from err

        if not _is_success(response.status_code):
            raise JenkinsError(
                f"HTTP {response.status_code} for {self._describe(url)}"
            )

        if len(response.content) > max_bytes:
            raise JenkinsError(f"config.xml too large for {self._describe(url)}")

        return response.text

    def get_last_build_number(self, job: str) -> int | None:
        data = self._get_json(
            self.job_url(job) + "api/json", {"tree": "lastBuild[number]"}
        )

        return (data.get("lastBuild") or {}).get("number")

    def _crumb_headers(self) -> dict[str, str]:
        # With API-token auth Jenkins does not require a crumb; when it does
        # exist (older/stricter setups) it must accompany a POST.
        try:
            response = self._http.get(self.base_url + "crumbIssuer/api/json")
        except httpx.HTTPError:
            return {}

        if response.status_code != 200:
            return {}

        try:
            data = response.json()
            return {data["crumbRequestField"]: data["crumb"]}
        except (ValueError, KeyError, TypeError):
            return {}

    def trigger_rebuild(self, job: str, parameters: dict[str, str]) -> str:
        """Queue a new build of `job` with `parameters`; returns the queue item URL.

        JenkinsError: Jenkins refused the request (nothing was queued).
        JenkinsUncertainError: the request may have been applied; do not retry.
        """

        if not self.allow_writes:
            raise JenkinsError("client is read-only")

        endpoint = "buildWithParameters" if parameters else "build"
        url = self.job_url(job) + endpoint
        self._check(url)
        headers = self._crumb_headers()

        try:
            response = self._http.post(
                url, data=parameters or None, headers=headers
            )
        except httpx.HTTPError as err:
            raise JenkinsUncertainError(
                f"{type(err).__name__} for {self._describe(url)}"
            ) from err

        if response.status_code not in (200, 201, 202):
            raise JenkinsError(
                f"HTTP {response.status_code} for {self._describe(url)}"
            )

        location = response.headers.get("location", "")
        queue_url = urljoin(url, location) if location else ""

        # Queued, but we cannot follow it: the caller still must not retry.
        if not (queue_url and _QUEUE_PATH.match(urlsplit(queue_url).path)
                and url_under_base(queue_url, self.base_url)):
            raise JenkinsUncertainError(
                f"build queued but no queue item for {self._describe(url)}"
            )

        return queue_url

    def get_run_status(self, build_url: str) -> tuple[bool, str | None, int | None]:
        """(building, result, duration_ms) of one build. Read-only."""

        data = self._get_json(build_url + "api/json", {"tree": "building,result,duration"})

        return bool(data.get("building")), data.get("result"), data.get("duration")

    def queue_item_state(self, queue_url: str) -> tuple[str, int | None, str | None]:
        """('queued' | 'started' | 'cancelled', build number, build url). Read-only."""

        if not (url_under_base(queue_url, self.base_url)
                and _QUEUE_PATH.match(urlsplit(queue_url).path)):
            raise JenkinsError("refusing queue URL outside Jenkins base")

        data = self._get_json(queue_url + "api/json")
        executable = data.get("executable") or {}

        if executable.get("number"):
            url = executable.get("url") or ""

            return "started", int(executable["number"]), (
                url if url_under_base(url, self.base_url) else None
            )

        if data.get("cancelled"):
            return "cancelled", None, None

        return "queued", None, None

    def find_build_by_queue_id(self, job: str, queue_id: int) -> tuple[int, str] | None:
        """The build Jenkins started from a queue item, found by its queueId.

        Jenkins forgets a queue item a few minutes after it leaves the queue;
        the build keeps its queueId. Read-only.
        """

        data = self._get_json(
            self.job_url(job) + "api/json",
            {"tree": "builds[number,url,queueId]{0,50}"},
        )

        for build in data.get("builds") or []:
            if build.get("queueId") == queue_id and url_under_base(build.get("url", ""), self.base_url):
                return int(build["number"]), build["url"]

        return None

    def resolve_queue_item(
        self, queue_url: str, timeout: float, poll: float
    ) -> tuple[int, str] | None:
        """(build number, build url) once the queue item starts, else None."""

        if not (url_under_base(queue_url, self.base_url)
                and _QUEUE_PATH.match(urlsplit(queue_url).path)):
            raise JenkinsError("refusing queue URL outside Jenkins base")

        deadline = time.monotonic() + timeout

        while True:
            data = self._get_json(queue_url + "api/json")
            executable = data.get("executable") or {}

            if executable.get("number"):
                number = int(executable["number"])
                url = executable.get("url") or ""

                if not url_under_base(url, self.base_url):
                    return number, ""

                return number, url

            if data.get("cancelled") or time.monotonic() >= deadline:
                return None

            _sleep(poll)

    def list_jobs(self) -> list[JobRef]:
        """Every job under the base, folders flattened, in one request."""

        data = self._get_json(
            self.base_url + "api/json", {"tree": _jobs_tree(_JOB_TREE_DEPTH)}
        )

        found: list[JobRef] = []

        def walk(nodes):
            for node in nodes or []:
                if node.get("jobs") is not None:
                    walk(node["jobs"])
                    continue

                name = node.get("fullName")

                if not name:
                    continue

                last = node.get("lastCompletedBuild") or {}
                color = node.get("color") or ""

                found.append(JobRef(
                    full_name=name,
                    disabled=color.startswith("disabled"),
                    last_number=last.get("number"),
                    last_result=last.get("result"),
                    last_timestamp=last.get("timestamp"),
                ))

        walk(data.get("jobs"))

        return found


def jenkins_client_from_settings(write: bool = False) -> JenkinsClient:
    """write=True builds the executor's client (may POST a rebuild)."""

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
        allow_writes=write,
    )
