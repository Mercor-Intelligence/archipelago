"""Jenkins Field Check Verifier - File-based verification for Jenkins state.

Parses Jenkins configuration files (XML, manifests, logs) directly from the
snapshot ZIP archive. No Jenkins installation or external dependencies required
- pure XML parsing via xml.etree.ElementTree (stdlib).

Jenkins stores all state as files under $JENKINS_HOME:
  - config.xml: global configuration (security, views, nodes)
  - jobs/*/config.xml: job/pipeline definitions
  - jobs/*/builds/*/build.xml: build results and metadata
  - jobs/*/builds/*/log: console output
  - jobs/*/builds/*/workflow/*.xml: pipeline stage execution data
  - plugins/*.jpi: installed plugins (JAR manifests)
  - users/*/config.xml: user accounts
  - credentials.xml: stored credentials
  - nodes/*/config.xml: build agent configuration
"""

import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

JENKINS_HOME_BASES = [
    ".apps_data/jenkins_browser/jenkins_home",
    ".apps_data/jenkins/jenkins_home",
]

# Map of root XML element tags to job types
JOB_TYPE_MAP = {
    "project": "freestyle",
    "flow-definition": "pipeline",
    "com.cloudbees.hudson.plugins.folder.Folder": "folder",
    "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject": "multibranch",
    "maven2-moduleset": "maven",
}

# Map of credential class name fragments to types
CRED_TYPE_MAP = {
    "BasicSSHUserPrivateKey": "ssh",
    "UsernamePasswordCredentialsImpl": "username_password",
    "StringCredentialsImpl": "secret_text",
    "FileCredentialsImpl": "secret_file",
    "CertificateCredentialsImpl": "certificate",
}

# Map of trigger class name fragments to types
TRIGGER_TYPE_MAP = {
    "TimerTrigger": "cron",
    "SCMTrigger": "scm_poll",
    "ReverseBuildTrigger": "upstream",
    "GitHubPushTrigger": "github_push",
}

# Map of publisher class name fragments to types
PUBLISHER_TYPE_MAP = {
    "Mailer": "email",
    "ArtifactArchiver": "archive_artifacts",
    "JUnitResultArchiver": "junit",
    "BuildTrigger": "downstream",
    "Fingerprinter": "fingerprint",
}


# ============================================================================
# SNAPSHOT HELPERS
# ============================================================================


def _find_jenkins_home(snapshot_zip: zipfile.ZipFile) -> str | None:
    """Find the Jenkins home directory path inside the snapshot ZIP."""
    names = snapshot_zip.namelist()
    for base in JENKINS_HOME_BASES:
        config = f"{base}/config.xml"
        if config in names:
            return base
    # Fallback: look for any path ending in jenkins_home/config.xml
    for name in names:
        if name.endswith("jenkins_home/config.xml"):
            return name.rsplit("/config.xml", 1)[0]
    return None


def _read_xml(snapshot_zip: zipfile.ZipFile, path: str) -> ET.Element | None:
    """Read and parse an XML file from the snapshot ZIP."""
    try:
        xml_bytes = snapshot_zip.read(path)
        return ET.fromstring(xml_bytes)
    except (KeyError, ET.ParseError, Exception) as e:
        logger.debug(f"Could not parse {path}: {e}")
        return None


def _read_text(
    snapshot_zip: zipfile.ZipFile, path: str, max_bytes: int = 500_000
) -> str:
    """Read a text file from the snapshot ZIP."""
    try:
        data = snapshot_zip.read(path)
        return data[:max_bytes].decode("utf-8", errors="replace")
    except (KeyError, Exception):
        return ""


def _text(element: ET.Element | None, tag: str, default: str = "") -> str:
    """Get text content of a child element."""
    if element is None:
        return default
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else default


def _list_dirs(snapshot_zip: zipfile.ZipFile, parent: str) -> list[str]:
    """List immediate subdirectory names under a path in the ZIP."""
    prefix = parent.rstrip("/") + "/"
    dirs: set[str] = set()
    for name in snapshot_zip.namelist():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
    return sorted(dirs)


# ============================================================================
# JOB PARSING
# ============================================================================


def _job_dir(jenkins_home: str, job_name: str) -> str:
    """Resolve the filesystem path for a job, handling folder nesting.

    "My Job"       -> <jenkins_home>/jobs/My Job
    "folder/job"   -> <jenkins_home>/jobs/folder/jobs/job
    """
    segments = "/jobs/".join(job_name.split("/"))
    return f"{jenkins_home}/jobs/{segments}"


def _get_job_config(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str, job_name: str
) -> ET.Element | None:
    """Read a job's config.xml."""
    return _read_xml(snapshot_zip, f"{_job_dir(jenkins_home, job_name)}/config.xml")


def _get_job_type(root: ET.Element) -> str:
    """Determine job type from root element tag."""
    return JOB_TYPE_MAP.get(root.tag, root.tag)


def _job_exists(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str, job_name: str
) -> bool:
    """Check if a job exists."""
    return _get_job_config(snapshot_zip, jenkins_home, job_name) is not None


def _list_jobs(snapshot_zip: zipfile.ZipFile, jenkins_home: str) -> list[str]:
    """List all top-level job names."""
    return _list_dirs(snapshot_zip, f"{jenkins_home}/jobs")


def _get_pipeline_script(root: ET.Element) -> str:
    """Extract pipeline Groovy script from a pipeline job config."""
    defn = root.find("definition")
    if defn is not None:
        script_el = defn.find("script")
        if script_el is not None and script_el.text:
            return script_el.text.strip()
    return ""


# ============================================================================
# BUILD PARSING
# ============================================================================


def _get_build_xml(
    snapshot_zip: zipfile.ZipFile,
    jenkins_home: str,
    job_name: str,
    build_number: str,
) -> ET.Element | None:
    """Read a build's build.xml."""
    path = f"{_job_dir(jenkins_home, job_name)}/builds/{build_number}/build.xml"
    return _read_xml(snapshot_zip, path)


def _get_console_output(
    snapshot_zip: zipfile.ZipFile,
    jenkins_home: str,
    job_name: str,
    build_number: str,
) -> str:
    """Read a build's console log."""
    path = f"{_job_dir(jenkins_home, job_name)}/builds/{build_number}/log"
    return _read_text(snapshot_zip, path)


def _list_builds(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str, job_name: str
) -> list[str]:
    """List build numbers for a job."""
    builds_dir = f"{_job_dir(jenkins_home, job_name)}/builds"
    dirs = _list_dirs(snapshot_zip, builds_dir)
    # Filter to numeric build numbers
    return [d for d in dirs if d.isdigit()]


# ============================================================================
# PIPELINE STAGE PARSING
# ============================================================================


def _get_stages(
    snapshot_zip: zipfile.ZipFile,
    jenkins_home: str,
    job_name: str,
    build_number: str,
) -> list[dict[str, str]]:
    """Extract pipeline stage info from workflow/*.xml files.

    Returns list of dicts with keys: name, status.
    """
    workflow_dir = f"{_job_dir(jenkins_home, job_name)}/builds/{build_number}/workflow"
    stages = []
    wf_files = sorted(
        name
        for name in snapshot_zip.namelist()
        if name.startswith(workflow_dir + "/") and name.endswith(".xml")
    )
    for wf_path in wf_files:
        root = _read_xml(snapshot_zip, wf_path)
        if root is None:
            continue

        stage_name = ""
        status = ""

        # Find stage name in any element containing stageName
        for elem in root.iter():
            sn = elem.find("stageName")
            if sn is not None and sn.text:
                stage_name = sn.text.strip()
            if elem.tag == "stageName" and elem.text:
                stage_name = elem.text.strip()

        # Find result/status
        result_el = root.find(".//result")
        if result_el is not None:
            status_el = result_el.find("status")
            if status_el is not None and status_el.text:
                status = status_el.text.strip()
            elif result_el.text:
                status = result_el.text.strip()

        if stage_name:
            stages.append({"name": stage_name, "status": status})

    return stages


# ============================================================================
# PLUGIN PARSING
# ============================================================================


def _get_plugins(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> list[dict[str, str]]:
    """Extract plugin info from plugins/*.jpi manifest files."""
    plugins_dir = f"{jenkins_home}/plugins/"
    seen = set()
    plugins = []

    for name in sorted(snapshot_zip.namelist()):
        if not name.startswith(plugins_dir):
            continue
        # Match *.jpi and *.hpi files at the plugins/ level
        rest = name[len(plugins_dir) :]
        if "/" in rest:
            continue
        if not (rest.endswith(".jpi") or rest.endswith(".hpi")):
            continue

        short_name = rest.rsplit(".", 1)[0]
        if short_name in seen:
            continue
        seen.add(short_name)

        version = ""
        long_name = ""
        try:
            import io as _io

            plugin_bytes = snapshot_zip.read(name)
            with zipfile.ZipFile(_io.BytesIO(plugin_bytes), "r") as pzip:
                manifest = pzip.read("META-INF/MANIFEST.MF").decode(
                    "utf-8", errors="replace"
                )
                for line in manifest.splitlines():
                    if line.startswith("Plugin-Version:"):
                        version = line.split(":", 1)[1].strip()
                    elif line.startswith("Long-Name:"):
                        long_name = line.split(":", 1)[1].strip()
                    elif line.startswith("Short-Name:"):
                        short_name = line.split(":", 1)[1].strip()
        except Exception:
            pass

        # Check for .disabled marker
        disabled_path = f"{name}.disabled"
        enabled = "false" if disabled_path in snapshot_zip.namelist() else "true"

        plugins.append(
            {
                "short_name": short_name,
                "version": version,
                "long_name": long_name,
                "enabled": enabled,
            }
        )

    return plugins


def _plugin_exists(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str, plugin_name: str
) -> bool:
    """Check if a plugin is installed."""
    plugins = _get_plugins(snapshot_zip, jenkins_home)
    return any(p["short_name"] == plugin_name for p in plugins)


def _plugin_enabled(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str, plugin_name: str
) -> bool:
    """Check if a plugin is installed and enabled."""
    plugins = _get_plugins(snapshot_zip, jenkins_home)
    return any(
        p["short_name"] == plugin_name and p["enabled"] == "true" for p in plugins
    )


# ============================================================================
# USER PARSING
# ============================================================================


def _get_users(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> list[dict[str, str]]:
    """Extract user info from users/*/config.xml."""
    users_dir = f"{jenkins_home}/users"
    users = []
    for user_dir_name in _list_dirs(snapshot_zip, users_dir):
        config_path = f"{users_dir}/{user_dir_name}/config.xml"
        root = _read_xml(snapshot_zip, config_path)
        if root is None:
            continue
        user_id = _text(root, "id", user_dir_name)
        full_name = _text(root, "fullName", user_dir_name)
        email = ""
        for prop in root.iter():
            if "Mailer" in prop.tag:
                addr = prop.find("emailAddress")
                if addr is not None and addr.text:
                    email = addr.text.strip()
        users.append({"id": user_id, "full_name": full_name, "email": email})
    return users


# ============================================================================
# CREDENTIAL PARSING
# ============================================================================


def _get_credentials(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> list[dict[str, str]]:
    """Extract credential info from credentials.xml."""
    cred_path = f"{jenkins_home}/credentials.xml"
    root = _read_xml(snapshot_zip, cred_path)
    if root is None:
        return []

    creds = []
    seen_ids: set[str] = set()
    for elem in root.iter():
        tag = elem.tag
        cls = elem.get("class", "")
        for suffix, ctype in CRED_TYPE_MAP.items():
            if suffix in tag or suffix in cls:
                cred_id = _text(elem, "id")
                if cred_id and cred_id not in seen_ids:
                    seen_ids.add(cred_id)
                    creds.append(
                        {
                            "id": cred_id,
                            "type": ctype,
                            "scope": _text(elem, "scope", "GLOBAL"),
                            "description": _text(elem, "description"),
                        }
                    )
                break
    return creds


# ============================================================================
# NODE PARSING
# ============================================================================


def _get_nodes(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> list[dict[str, str]]:
    """Extract node info from config.xml and nodes/*/config.xml."""
    nodes = []

    # Built-in node
    config_path = f"{jenkins_home}/config.xml"
    root = _read_xml(snapshot_zip, config_path)
    if root is not None:
        nodes.append(
            {
                "name": "built-in",
                "num_executors": _text(root, "numExecutors", "2"),
                "mode": _text(root, "mode", "NORMAL"),
                "labels": _text(root, "label"),
            }
        )

    # Agent nodes
    nodes_dir = f"{jenkins_home}/nodes"
    for node_name in _list_dirs(snapshot_zip, nodes_dir):
        node_config = f"{nodes_dir}/{node_name}/config.xml"
        nroot = _read_xml(snapshot_zip, node_config)
        if nroot is None:
            continue
        nodes.append(
            {
                "name": _text(nroot, "name", node_name),
                "num_executors": _text(nroot, "numExecutors", "1"),
                "mode": _text(nroot, "mode", "NORMAL"),
                "labels": _text(nroot, "label"),
            }
        )

    return nodes


# ============================================================================
# VIEW PARSING
# ============================================================================


def _get_views(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> list[dict[str, Any]]:
    """Extract view info from config.xml."""
    config_path = f"{jenkins_home}/config.xml"
    root = _read_xml(snapshot_zip, config_path)
    if root is None:
        return []

    views_el = root.find("views")
    if views_el is None:
        return []

    views = []
    for view_el in views_el:
        tag = view_el.tag
        view_type = tag.rsplit(".", 1)[-1] if "." in tag else tag
        view_name = _text(view_el, "name")
        if not view_name:
            continue

        job_list = []
        job_names_el = view_el.find("jobNames")
        if job_names_el is not None:
            for s in job_names_el.findall("string"):
                if s.text:
                    job_list.append(s.text.strip())

        views.append(
            {
                "name": view_name,
                "type": view_type,
                "jobs": job_list,
            }
        )

    return views


# ============================================================================
# GLOBAL CONFIG PARSING
# ============================================================================


def _get_global_config(
    snapshot_zip: zipfile.ZipFile, jenkins_home: str
) -> dict[str, str]:
    """Extract key global config values from config.xml."""
    config_path = f"{jenkins_home}/config.xml"
    root = _read_xml(snapshot_zip, config_path)
    if root is None:
        return {}

    config: dict[str, str] = {}

    sr = root.find("securityRealm")
    if sr is not None:
        config["security_realm"] = sr.get("class", "")

    auth = root.find("authorizationStrategy")
    if auth is not None:
        config["authorization_strategy"] = auth.get("class", "")

    config["num_executors"] = _text(root, "numExecutors", "2")
    config["use_security"] = _text(root, "useSecurity", "true")

    crumb = root.find("crumbIssuer")
    config["csrf_enabled"] = "true" if crumb is not None else "false"

    config["primary_view"] = _text(root, "primaryView", "all")
    config["system_message"] = _text(root, "systemMessage", "")

    return config


# ============================================================================
# TRIGGER AND SCM PARSING
# ============================================================================


def _get_triggers(
    root: ET.Element,
) -> list[dict[str, str]]:
    """Extract build triggers from a job config.xml root."""
    triggers_el = root.find("triggers")
    if triggers_el is None:
        return []

    results = []
    for trigger_el in triggers_el:
        tag = trigger_el.tag
        trigger_type = tag
        for suffix, ttype in TRIGGER_TYPE_MAP.items():
            if suffix in tag:
                trigger_type = ttype
                break

        schedule = ""
        spec = trigger_el.find("spec")
        if spec is not None and spec.text:
            schedule = spec.text.strip()

        upstream_job = ""
        upstream = trigger_el.find("upstreamProjects")
        if upstream is not None and upstream.text:
            upstream_job = upstream.text.strip()

        results.append(
            {
                "type": trigger_type,
                "schedule": schedule,
                "upstream_job": upstream_job,
            }
        )

    return results


def _get_scm_config(root: ET.Element) -> dict[str, str] | None:
    """Extract SCM configuration from a job config.xml root."""
    scm = root.find("scm")
    if scm is None:
        return None

    scm_class = scm.get("class", "")
    if "NullSCM" in scm_class:
        return None

    scm_type = (
        "git"
        if "Git" in scm_class or "git" in scm_class
        else scm_class.rsplit(".", 1)[-1]
    )

    url = ""
    branch = ""
    for remote in scm.iter():
        url_el = remote.find("url")
        if url_el is not None and url_el.text:
            url = url_el.text.strip()
        branch_el = remote.find("name")
        if branch_el is not None and branch_el.text and "/" in (branch_el.text or ""):
            branch = branch_el.text.strip()

    return {"type": scm_type, "url": url, "branch": branch}


# ============================================================================
# POST-BUILD ACTION PARSING
# ============================================================================


def _get_publishers(root: ET.Element) -> list[dict[str, str]]:
    """Extract post-build actions from a job config.xml root."""
    publishers_el = root.find("publishers")
    if publishers_el is None:
        return []

    results = []
    for pub in publishers_el:
        tag = pub.tag
        action_type = tag
        for suffix, atype in PUBLISHER_TYPE_MAP.items():
            if suffix in tag:
                action_type = atype
                break

        config_str = ""
        if action_type == "email":
            el = pub.find("recipients")
            if el is not None and el.text:
                config_str = el.text.strip()
        elif action_type == "archive_artifacts":
            el = pub.find("artifacts")
            if el is not None and el.text:
                config_str = el.text.strip()
        elif action_type == "junit":
            el = pub.find("testResults")
            if el is not None and el.text:
                config_str = el.text.strip()
        elif action_type == "downstream":
            el = pub.find("childProjects")
            if el is not None and el.text:
                config_str = el.text.strip()

        results.append({"type": action_type, "config": config_str})

    return results


# ============================================================================
# FIELD MAPPINGS
# ============================================================================

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # --- Jobs ---
    "Job Exists": {
        "category": "job",
        "parser": "job_exists",
        "required_params": ["job_name"],
        "result_type": "boolean",
    },
    "Job Count": {
        "category": "job",
        "parser": "job_count",
        "required_params": [],
        "result_type": "integer",
    },
    "Job Type": {
        "category": "job",
        "parser": "job_type",
        "required_params": ["job_name"],
        "result_type": "string",
    },
    "Job Disabled": {
        "category": "job",
        "parser": "job_disabled",
        "required_params": ["job_name"],
        "result_type": "boolean",
    },
    "Pipeline Script Contains": {
        "category": "job",
        "parser": "pipeline_script_contains",
        "required_params": ["job_name", "expected_text"],
        "result_type": "boolean",
    },
    # --- Builds ---
    "Build Exists": {
        "category": "build",
        "parser": "build_exists",
        "required_params": ["job_name", "build_number"],
        "result_type": "boolean",
    },
    "Build Result": {
        "category": "build",
        "parser": "build_result",
        "required_params": ["job_name", "build_number"],
        "result_type": "string",
    },
    "Build Count": {
        "category": "build",
        "parser": "build_count",
        "required_params": ["job_name"],
        "result_type": "integer",
    },
    "Build Console Contains": {
        "category": "build",
        "parser": "build_console_contains",
        "required_params": ["job_name", "build_number", "expected_text"],
        "result_type": "boolean",
    },
    # --- Build Stages ---
    "Stage Exists": {
        "category": "stage",
        "parser": "stage_exists",
        "required_params": ["job_name", "build_number", "stage_name"],
        "result_type": "boolean",
    },
    "Stage Result": {
        "category": "stage",
        "parser": "stage_result",
        "required_params": ["job_name", "build_number", "stage_name"],
        "result_type": "string",
    },
    "Stage Count": {
        "category": "stage",
        "parser": "stage_count",
        "required_params": ["job_name", "build_number"],
        "result_type": "integer",
    },
    "All Stages Passed": {
        "category": "stage",
        "parser": "all_stages_passed",
        "required_params": ["job_name", "build_number"],
        "result_type": "boolean",
    },
    # --- Plugins ---
    "Plugin Installed": {
        "category": "plugin",
        "parser": "plugin_installed",
        "required_params": ["plugin_name"],
        "result_type": "boolean",
    },
    "Plugin Enabled": {
        "category": "plugin",
        "parser": "plugin_enabled",
        "required_params": ["plugin_name"],
        "result_type": "boolean",
    },
    "Plugin Count": {
        "category": "plugin",
        "parser": "plugin_count",
        "required_params": [],
        "result_type": "integer",
    },
    "Plugin Version": {
        "category": "plugin",
        "parser": "plugin_version",
        "required_params": ["plugin_name"],
        "result_type": "string",
    },
    # --- Users ---
    "User Exists": {
        "category": "user",
        "parser": "user_exists",
        "required_params": ["user_id"],
        "result_type": "boolean",
    },
    "User Count": {
        "category": "user",
        "parser": "user_count",
        "required_params": [],
        "result_type": "integer",
    },
    "User Full Name": {
        "category": "user",
        "parser": "user_full_name",
        "required_params": ["user_id"],
        "result_type": "string",
    },
    # --- Credentials ---
    "Credential Exists": {
        "category": "credential",
        "parser": "credential_exists",
        "required_params": ["credential_id"],
        "result_type": "boolean",
    },
    "Credential Type": {
        "category": "credential",
        "parser": "credential_type",
        "required_params": ["credential_id"],
        "result_type": "string",
    },
    # --- Nodes ---
    "Node Exists": {
        "category": "node",
        "parser": "node_exists",
        "required_params": ["node_name"],
        "result_type": "boolean",
    },
    "Node Executors": {
        "category": "node",
        "parser": "node_executors",
        "required_params": ["node_name"],
        "result_type": "integer",
    },
    "Node Count": {
        "category": "node",
        "parser": "node_count",
        "required_params": [],
        "result_type": "integer",
    },
    # --- Views ---
    "View Exists": {
        "category": "view",
        "parser": "view_exists",
        "required_params": ["view_name"],
        "result_type": "boolean",
    },
    "View Contains Job": {
        "category": "view",
        "parser": "view_contains_job",
        "required_params": ["view_name", "job_name"],
        "result_type": "boolean",
    },
    "View Count": {
        "category": "view",
        "parser": "view_count",
        "required_params": [],
        "result_type": "integer",
    },
    # --- Global Config ---
    "Config Value": {
        "category": "config",
        "parser": "config_value",
        "required_params": ["config_key"],
        "result_type": "string",
    },
    # --- SCM ---
    "SCM Configured": {
        "category": "scm",
        "parser": "scm_configured",
        "required_params": ["job_name"],
        "result_type": "boolean",
    },
    "SCM URL": {
        "category": "scm",
        "parser": "scm_url",
        "required_params": ["job_name"],
        "result_type": "string",
    },
    # --- Triggers ---
    "Trigger Exists": {
        "category": "trigger",
        "parser": "trigger_exists",
        "required_params": ["job_name", "trigger_type"],
        "result_type": "boolean",
    },
    "Trigger Schedule": {
        "category": "trigger",
        "parser": "trigger_schedule",
        "required_params": ["job_name", "trigger_type"],
        "result_type": "string",
    },
    # --- Post-Build Actions ---
    "Publisher Exists": {
        "category": "publisher",
        "parser": "publisher_exists",
        "required_params": ["job_name", "publisher_type"],
        "result_type": "boolean",
    },
}


# ============================================================================
# FIELD EVALUATION DISPATCH
# ============================================================================


def _evaluate_field(
    snapshot_zip: zipfile.ZipFile,
    jenkins_home: str,
    field_to_check: str,
    params: dict[str, Any],
) -> tuple[Any, str | None]:
    """Evaluate a field check and return (result, error_message)."""
    config = FIELD_MAPPINGS[field_to_check]
    parser = config["parser"]

    job_name = params.get("job_name", "")
    build_number = params.get("build_number", "")

    # --- Jobs ---
    if parser == "job_exists":
        return 1 if _job_exists(snapshot_zip, jenkins_home, job_name) else 0, None

    elif parser == "job_count":
        return len(_list_jobs(snapshot_zip, jenkins_home)), None

    elif parser == "job_type":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        return _get_job_type(root), None

    elif parser == "job_disabled":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        return 1 if _text(root, "disabled", "false") == "true" else 0, None

    elif parser == "pipeline_script_contains":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        script = _get_pipeline_script(root)
        expected = params.get("expected_text", "")
        return 1 if expected in script else 0, None

    # --- Builds ---
    elif parser == "build_exists":
        root = _get_build_xml(snapshot_zip, jenkins_home, job_name, build_number)
        return 1 if root is not None else 0, None

    elif parser == "build_result":
        root = _get_build_xml(snapshot_zip, jenkins_home, job_name, build_number)
        if root is None:
            return None, f"Build {job_name}#{build_number} not found"
        return _text(root, "result", "UNKNOWN"), None

    elif parser == "build_count":
        return len(_list_builds(snapshot_zip, jenkins_home, job_name)), None

    elif parser == "build_console_contains":
        console = _get_console_output(
            snapshot_zip, jenkins_home, job_name, build_number
        )
        if not console:
            return None, f"Console log for {job_name}#{build_number} not found"
        expected = params.get("expected_text", "")
        return 1 if expected in console else 0, None

    # --- Stages ---
    elif parser == "stage_exists":
        stages = _get_stages(snapshot_zip, jenkins_home, job_name, build_number)
        stage_name = params.get("stage_name", "")
        return 1 if any(s["name"] == stage_name for s in stages) else 0, None

    elif parser == "stage_result":
        stages = _get_stages(snapshot_zip, jenkins_home, job_name, build_number)
        stage_name = params.get("stage_name", "")
        for s in stages:
            if s["name"] == stage_name:
                return s["status"], None
        return None, f"Stage '{stage_name}' not found in {job_name}#{build_number}"

    elif parser == "stage_count":
        stages = _get_stages(snapshot_zip, jenkins_home, job_name, build_number)
        return len(stages), None

    elif parser == "all_stages_passed":
        stages = _get_stages(snapshot_zip, jenkins_home, job_name, build_number)
        if not stages:
            return 0, None
        return 1 if all(s["status"] == "SUCCESS" for s in stages) else 0, None

    # --- Plugins ---
    elif parser == "plugin_installed":
        return (
            1
            if _plugin_exists(snapshot_zip, jenkins_home, params.get("plugin_name", ""))
            else 0,
            None,
        )

    elif parser == "plugin_enabled":
        return (
            1
            if _plugin_enabled(
                snapshot_zip, jenkins_home, params.get("plugin_name", "")
            )
            else 0,
            None,
        )

    elif parser == "plugin_count":
        return len(_get_plugins(snapshot_zip, jenkins_home)), None

    elif parser == "plugin_version":
        plugins = _get_plugins(snapshot_zip, jenkins_home)
        pname = params.get("plugin_name", "")
        for p in plugins:
            if p["short_name"] == pname:
                return p["version"], None
        return None, f"Plugin '{pname}' not found"

    # --- Users ---
    elif parser == "user_exists":
        users = _get_users(snapshot_zip, jenkins_home)
        uid = params.get("user_id", "")
        return 1 if any(u["id"] == uid for u in users) else 0, None

    elif parser == "user_count":
        return len(_get_users(snapshot_zip, jenkins_home)), None

    elif parser == "user_full_name":
        users = _get_users(snapshot_zip, jenkins_home)
        uid = params.get("user_id", "")
        for u in users:
            if u["id"] == uid:
                return u["full_name"], None
        return None, f"User '{uid}' not found"

    # --- Credentials ---
    elif parser == "credential_exists":
        creds = _get_credentials(snapshot_zip, jenkins_home)
        cid = params.get("credential_id", "")
        return 1 if any(c["id"] == cid for c in creds) else 0, None

    elif parser == "credential_type":
        creds = _get_credentials(snapshot_zip, jenkins_home)
        cid = params.get("credential_id", "")
        for c in creds:
            if c["id"] == cid:
                return c["type"], None
        return None, f"Credential '{cid}' not found"

    # --- Nodes ---
    elif parser == "node_exists":
        nodes = _get_nodes(snapshot_zip, jenkins_home)
        nname = params.get("node_name", "")
        return 1 if any(n["name"] == nname for n in nodes) else 0, None

    elif parser == "node_executors":
        nodes = _get_nodes(snapshot_zip, jenkins_home)
        nname = params.get("node_name", "")
        for n in nodes:
            if n["name"] == nname:
                try:
                    return int(n["num_executors"]), None
                except ValueError:
                    return n["num_executors"], None
        return None, f"Node '{nname}' not found"

    elif parser == "node_count":
        return len(_get_nodes(snapshot_zip, jenkins_home)), None

    # --- Views ---
    elif parser == "view_exists":
        views = _get_views(snapshot_zip, jenkins_home)
        vname = params.get("view_name", "")
        return 1 if any(v["name"] == vname for v in views) else 0, None

    elif parser == "view_contains_job":
        views = _get_views(snapshot_zip, jenkins_home)
        vname = params.get("view_name", "")
        for v in views:
            if v["name"] == vname:
                return 1 if job_name in v["jobs"] else 0, None
        return None, f"View '{vname}' not found"

    elif parser == "view_count":
        return len(_get_views(snapshot_zip, jenkins_home)), None

    # --- Global Config ---
    elif parser == "config_value":
        cfg = _get_global_config(snapshot_zip, jenkins_home)
        key = params.get("config_key", "")
        if key not in cfg:
            return None, f"Config key '{key}' not found"
        return cfg[key], None

    # --- SCM ---
    elif parser == "scm_configured":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        return 1 if _get_scm_config(root) is not None else 0, None

    elif parser == "scm_url":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        scm = _get_scm_config(root)
        if scm is None:
            return None, f"No SCM configured for '{job_name}'"
        return scm["url"], None

    # --- Triggers ---
    elif parser == "trigger_exists":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        triggers = _get_triggers(root)
        ttype = params.get("trigger_type", "")
        return 1 if any(t["type"] == ttype for t in triggers) else 0, None

    elif parser == "trigger_schedule":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        triggers = _get_triggers(root)
        ttype = params.get("trigger_type", "")
        for t in triggers:
            if t["type"] == ttype:
                return t["schedule"], None
        return None, f"Trigger '{ttype}' not found on '{job_name}'"

    # --- Post-Build Actions ---
    elif parser == "publisher_exists":
        root = _get_job_config(snapshot_zip, jenkins_home, job_name)
        if root is None:
            return None, f"Job '{job_name}' not found"
        publishers = _get_publishers(root)
        ptype = params.get("publisher_type", "")
        return 1 if any(p["type"] == ptype for p in publishers) else 0, None

    return None, f"Unknown parser '{parser}' for field '{field_to_check}'"


# ============================================================================
# MAIN EVAL FUNCTION
# ============================================================================


async def jenkins_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify Jenkins state using file-based parsing.

    Parses XML configuration, build logs, and plugin manifests directly
    from the snapshot ZIP. No Jenkins installation required.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    def _error(msg: str) -> VerifierResult:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=msg,
        )

    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        return _error("field_to_check is required")

    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return _error("expected_value is required")

    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        return _error(f"Unknown field_to_check: {field_to_check}")

    # Collect required parameters
    params: dict[str, Any] = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            return _error(
                f"Required parameter '{param}' is missing for '{field_to_check}'"
            )
        params[param] = value

    if not input.final_snapshot_bytes:
        return _error("No final snapshot available")

    try:
        input.final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(input.final_snapshot_bytes, "r") as snapshot_zip:
            jenkins_home = _find_jenkins_home(snapshot_zip)
            if jenkins_home is None:
                return _error(
                    "Jenkins home not found in snapshot. "
                    "Expected config.xml under .apps_data/jenkins_browser/jenkins_home/"
                )

            result, error = _evaluate_field(
                snapshot_zip, jenkins_home, field_to_check, params
            )
    except zipfile.BadZipFile:
        return _error("Invalid snapshot zip file")
    except Exception as e:
        logger.error(f"Error evaluating {field_to_check}: {e}")
        return _error(f"Evaluation failed: {e!s}")

    if error:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": error, "field_checked": field_to_check},
            message=error,
        )

    # Compare result against expected value
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "boolean":
        actual_bool = (
            int(result) > 0 if isinstance(result, (int, float)) else bool(result)
        )
        expected_bool = str(expected_value).lower() in ("true", "1", "yes")
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'}"
    elif result_type == "integer":
        try:
            actual_int = int(result)
            expected_int = int(expected_value)
            matches = actual_int == expected_int
        except (ValueError, TypeError) as e:
            return _error(
                f"Invalid integer value - result: {result}, "
                f"expected: {expected_value}. Error: {e!s}"
            )
        message = f"{field_to_check}: {actual_int} (expected {expected_int})"
    elif result_type == "string":
        actual_str = str(result).strip()
        expected_str = str(expected_value).strip()
        matches = actual_str.lower() == expected_str.lower()
        message = f"{field_to_check}: '{actual_str}' (expected '{expected_str}')"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} (expected {expected_value})"

    logger.info(
        f"Jenkins field check: {field_to_check} = {result} "
        f"(expected: {expected_value}) -> {matches}"
    )

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
