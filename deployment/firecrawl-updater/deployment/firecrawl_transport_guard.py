"""Read-only GA401 public-page transport guard.

Audited transport profiles are frozen below. Rebuilding an image does not
change admission when all audited code, dependencies and configuration match.
Never learn expectations from a running image: unknown transport changes
require an audit. This checks the public-fetch boundary, not whole-image
provenance, adapter deployment or release authorization.
"""
import json
import os
import re
import subprocess
import sys
import time


CONTAINER = "firecrawl-api-1"
RECEIPT_SHA256 = "415b43bbc4afe0e618b9fb219a8c396adb5200383b4c5b443a3a6b0dfa5d2e84"
EXPECTED_HASHES = {
    "/app/dist/src/scraper/scrapeURL/engines/index.js": "b99e5feb50de79dab5fa1593cd57ed77c675f04fc672f32201322643aa8561fb",
    "/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js": "5cedde8cdee6c01ee06e6c11e0dfb70b0779edc2ed18007c67f8c6a54ab5ca6b",
    "/app/dist/src/scraper/scrapeURL/engines/utils/downloadFile.js": "ae369ac5445de246e3eecc26724f0ed78b9f5d40e66c6c5c89ef43da82b26c32",
    "/app/dist/src/scraper/scrapeURL/engines/fetch/index.js": "be55e3e72815347c8095a376d81f1ffa1e9ea826229de14230b86549f2a8fdd0",
    "/app/dist/src/scraper/scrapeURL/engines/pdf/index.js": "3fb20bb3e32ca0e062d4389745d52369f32408d7551b2edb13ce789377aa43ff",
    "/app/dist/src/scraper/scrapeURL/engines/document/index.js": "9ee8d7745dbc40ae9715338feecb15adc3bff488818b667929ace8350059bc62",
    "/app/dist/src/scraper/scrapeURL/index.js": "17a408e4f51dace4013d36ff2bd9146226e4af8d0548042ea22ed96481981755",
    "/app/dist/src/controllers/v2/types.js": "939c62b275ea672571d062587f4d26f9cbf3a24d0ed0fa98fb7deeb8e5351960",
    "/app/dist/src/lib/exchange.js": "0a45d1556fbec12e2bf828950af849df46c635b6f19dff40dee9ea0a93e81762",
    "undici/client.js": "923945cc690e25d8cb7f33319fc0b2f98396a3a9b42be3d7c85cb69db95cf4e3",
    "ipaddr.js": "75c602c8834cc9e1bcdb7d1d2eaadbbd5ba0e19660d9ea3f450dbf17f2ec8495",
}
TRANSPORT_PROFILES = {
    "v2.11.408": {
        "receipt_sha256": RECEIPT_SHA256,
        "hashes": EXPECTED_HASHES,
        "dependencies": {"node_major": 22, "undici": "7.29.0", "ipaddr_js": "2.2.0"},
    },
    "v2.11.470": {
        "receipt_sha256": "a2005efe288ff4e6c3fa0da4bd376af32c70e469a58b0731d5f5deca302165f5",
        "hashes": {
            **EXPECTED_HASHES,
            "/app/dist/src/scraper/scrapeURL/engines/index.js": "594ce21c4af8d20de17129496acbd7893c3696dd6a0e8e9ea8545aede110f293",
            "/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js": "6def9e69cf39a78132fac52ae27550fab45de90fe0f1a9b0115bfc0149ee0a2f",
            "/app/dist/src/scraper/scrapeURL/engines/pdf/index.js": "d977814d27259db46c7db87ba3930cd3b13cf5b3abd2d2290c4a57792e6cc30f",
            "/app/dist/src/scraper/scrapeURL/index.js": "7bda4d1c9d3c391835ef140c2468215fed76dd781e43fe571d927c895dc6490c",
            "/app/dist/src/controllers/v2/types.js": "31d68073299efe0439b705e21d214087330a3b9fb67af05755708b6aad87ddcd",
            "/app/dist/src/lib/exchange.js": "a39533fbcd7910edd025fbc172cb79769569294435303185a9bb9bf5c3edf4fc",
            "undici/client.js": "07529b464fb6be9b42bd4a34d2ea0e090812b237ec3a1f047ab94df052fa428f",
        },
        "dependencies": {"node_major": 22, "undici": "7.29.1", "ipaddr_js": "2.2.0"},
    },
}
# Frozen transport audit: deployment/firecrawl-transport-20261009.json.
# This profile certifies the static-fetch boundary, not the complete image.
TRANSPORT_PROFILES["v2.11.473"] = {
    "receipt_sha256": "a208df1d1506d20ad751a5c080689efc0545eb88fb64fb7cd6e51a879226f23c",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.470"]["hashes"],
        "/app/dist/src/scraper/scrapeURL/engines/index.js": "2f9c011ee5076bfee0ea29bd5aeb177a8f5b71a67f159f84c139de239d59fc89",
        "/app/dist/src/scraper/scrapeURL/index.js": "8baf874551e0d9512cd9487f29ecddbf0410019777b36aec611ac66bae77f95a",
        "/app/dist/src/lib/exchange.js": "5251d2daef9feac6c9189b29e32ac32f8f5881fb5ab76a4f4e258c3f0feda781",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.0", "ipaddr_js": "2.2.0"},
}
# Frozen candidate audit: deployment/firecrawl-transport-20261014.json.
# Candidate-only: independent review and deployment admission are still required.
TRANSPORT_PROFILES["v2.11.518"] = {
    "receipt_sha256": "37f319c9111041ae9fa4ef42bec541edae28f4a0cb303fc06312635f461757b6",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.473"]["hashes"],
        "/app/dist/src/controllers/v2/types.js": "119ba71fb0be434a390fa3985f56d185971421402362ea25c7e1fc7ac2609143",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.0", "ipaddr_js": "2.2.0"},
}
# Frozen candidate audit: deployment/firecrawl-transport-20261029.json.
# This profile certifies only the reviewed public static-fetch boundary.
TRANSPORT_PROFILES["v2.11.621"] = {
    "receipt_sha256": "bb3df6baf20484a8e86204e843809a70b1d017218ce07e7c518d4fedc2ea8ce7",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.518"]["hashes"],
        "/app/dist/src/scraper/scrapeURL/engines/index.js": "48cb48735d182bc8869840efceb824646e713e7a0fd9af6fb60706c0bf6c8bd7",
        "/app/dist/src/scraper/scrapeURL/engines/pdf/index.js": "4579c1ee7ffa5097000582588be4566f4d9b81ce837a7e676b25e309ad6b7762",
        "/app/dist/src/scraper/scrapeURL/index.js": "efc3d636a8c996d39303f62976b559bd9cb85092e9b5750c41bbfe79aa4f4aee",
        "/app/dist/src/controllers/v2/types.js": "f1b84bdbd3d23f21ed3b45700049e9a9b840bae0c1bff7737efa76bdd7a931a2",
        "undici/client.js": "84205dc96cb9bf7432c4b3e80f0babaa11d6f4faf24ead3e7d9660e9d8fee0b3",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.1", "ipaddr_js": "2.2.0"},
    "undici_lib_sha256": "213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c",
}
# Frozen public-fetch audit: deployment/firecrawl-transport-20261030.json.
# Dependency admission and release checks remain separate requirements.
TRANSPORT_PROFILES["v2.11.625"] = {
    "receipt_sha256": "52372a3d41d11cddb37869d0dea62f74e72fc790a126c89188fc42437a0103aa",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.621"]["hashes"],
        "/app/dist/src/controllers/v2/types.js": "f78ad7dc0f60ef4babcd1e794414560b48b82c40d0035544637f9b29d0e5ee72",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.1", "ipaddr_js": "2.2.0"},
    "undici_lib_sha256": "213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c",
}
# Frozen 629 public-fetch audit; complete profile only, not release approval.
TRANSPORT_PROFILES["v2.11.629"] = {
    "receipt_sha256": "0d37bc069d94b8c6e1e88b62665640720ed3755c6be12e75f9d9a46c3d708be9",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.625"]["hashes"],
        "/app/dist/src/controllers/v2/types.js": "916db660b9704b4a2b4a54e391f210a3db66f7314d75b01d74be140f8a80b124",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.1", "ipaddr_js": "2.2.0"},
    "undici_lib_sha256": "213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c",
}
EXPECTED_PREDICATES = {
    "ALLOW_LOCAL_WEBHOOKS_true": False,
    "PROXY_SERVER_nonempty": False,
    "FIRE_ENGINE_BETA_URL_nonempty": False,
    "INDEX_DATABASE_URL_nonempty": False,
    "WIKIPEDIA_ENTERPRISE_USERNAME_nonempty": False,
    "WIKIPEDIA_ENTERPRISE_PASSWORD_nonempty": False,
    "XAI_API_KEY_nonempty": False,
    "USE_DB_AUTHENTICATION_true": False,
    "FIRE_PDF_BASE_URL_nonempty": False,
    "FIRE_EXCHANGE_URL_nonempty": False,
    "FORCED_ENGINE_DOMAINS_nonempty": False,
    "PLAYWRIGHT_MICROSERVICE_URL_configured": True,
}
TOTAL_TIMEOUT = 30
COMMAND_TIMEOUT = 10
MAX_OUTPUT = 16384
INSPECT_FORMAT = '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}}}'


class GuardError(Exception):
    def __init__(self, stage, code, mismatches=()):
        self.stage, self.code, self.mismatches = stage, code, list(mismatches)


def run_json(arguments, stage, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GuardError(stage, "timeout")
    try:
        result = subprocess.run(arguments, capture_output=True, text=True,
                                encoding="utf-8", errors="strict",
                                timeout=min(COMMAND_TIMEOUT, remaining), check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise GuardError(stage, "timeout") from None
    except (OSError, UnicodeError):
        raise GuardError(stage, "command_unavailable") from None
    # Never emit Docker stderr, exception messages, or raw child output.
    if result.returncode:
        raise GuardError(stage, "command_failed")
    if len(result.stdout) > MAX_OUTPUT:
        raise GuardError(stage, "output_limit")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise GuardError(stage, "invalid_json") from None
    if not isinstance(data, dict):
        raise GuardError(stage, "invalid_shape")
    return data


def verify_container(data, stage):
    if set(data) != {"id", "image", "running"}:
        raise GuardError(stage, "invalid_shape")
    identity = data["id"]
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise GuardError(stage, "invalid_container_identity")
    if (not isinstance(data["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", data["image"])
            or data["running"] is not True):
        raise GuardError(stage, "container_mismatch")
    return identity


def probe_source():
    # This Node process reads only fixed audited code files and computes named
    # booleans. It imports no Firecrawl service modules and sends no requests.
    return """
const fs = require('fs'), crypto = require('crypto'), path = require('path');
const load = require('module').createRequire('/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js');
const paths = PATHS, predicates = PREDICATES;
try {
  const undiciPackage = load.resolve('undici/package.json');
  const ipaddrPackage = load.resolve('ipaddr.js/package.json');
  const dependencies = {
    node_major: Number(process.versions.node.split('.')[0]),
    undici: JSON.parse(fs.readFileSync(undiciPackage, 'utf8')).version,
    ipaddr_js: JSON.parse(fs.readFileSync(ipaddrPackage, 'utf8')).version
  };
  const hashes = {};
  for (const name of paths) {
    const resolved = name === 'undici/client.js' ? path.join(path.dirname(undiciPackage), 'lib/dispatcher/client.js')
      : name === 'ipaddr.js' ? load.resolve('ipaddr.js') : name;
    const stat = fs.statSync(resolved);
    if (!stat.isFile() || stat.size > 1048576) throw new Error();
    hashes[name] = crypto.createHash('sha256').update(fs.readFileSync(resolved)).digest('hex');
  }
  const actual = {};
  for (const name of Object.keys(predicates)) {
    const split = name.lastIndexOf('_'), key = name.slice(0, split), kind = name.slice(split + 1);
    actual[name] = kind === 'true' ? process.env[key] === 'true' : !!process.env[key];
  }
  // Zod stringbool also accepts truthy spellings such as '1' or 'yes'.
  // Require the audited safe literals, not merely the absence of 'true'.
  const safeguards = {
    private_bypass_absent_or_empty: !process.env.ALLOW_LOCAL_WEBHOOKS,
    db_auth_canonical_false: process.env.USE_DB_AUTHENTICATION === 'false',
    dotenv_absent: !fs.existsSync('/app/.env'),
    alternate_dotenv_unconfigured: !process.env.DOTENV_CONFIG_PATH,
    expected_working_directory: process.cwd() === '/app',
    node_options_unconfigured: !process.env.NODE_OPTIONS
  };
  console.log(JSON.stringify({hashes, dependencies, predicates: actual, safeguards}));
} catch (_) {
  process.exitCode = 1;
}
""".replace("PATHS", json.dumps(list(EXPECTED_HASHES))).replace("PREDICATES", json.dumps(EXPECTED_PREDICATES))


def undici_tree_probe_source():
    # Bind the entire audited Undici lib tree for profiles that require it.
    # This reads static files only; reject links, special files, and excessive input.
    return """
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const load = require('module').createRequire('/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js');
try {
  const root = path.join(path.dirname(load.resolve('undici/package.json')), 'lib');
  if (!fs.lstatSync(root).isDirectory()) throw new Error();
  const entries = []; let total = 0;
  function walk(dir) {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name), stat = fs.lstatSync(full);
      if (stat.isSymbolicLink()) throw new Error();
      if (stat.isDirectory()) walk(full);
      else if (stat.isFile()) {
        total += stat.size;
        if (stat.size > 1048576 || total > 33554432) throw new Error();
        const digest = crypto.createHash('sha256').update(fs.readFileSync(full)).digest('hex');
        entries.push([path.relative(root, full).split(path.sep).join('/'), digest]);
        if (entries.length > 2048) throw new Error();
      } else throw new Error();
    }
  }
  walk(root);
  entries.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  const digest = crypto.createHash('sha256').update(JSON.stringify(entries)).digest('hex');
  console.log(JSON.stringify({undici_lib_sha256: digest, file_count: entries.length}));
} catch (_) { process.exitCode = 1; }
"""


def _preflight():
    deadline = time.monotonic() + TOTAL_TIMEOUT
    arguments = ["docker", "inspect", "--format", INSPECT_FORMAT, CONTAINER]
    before = run_json(arguments, "container_before", deadline)
    identity = verify_container(before, "container_before")
    # Pin the exec to the inspected container ID, not a potentially rebound name.
    probe = run_json(["docker", "exec", identity, "node", "-e", probe_source()], "code_and_config", deadline)
    if set(probe) != {"hashes", "dependencies", "predicates", "safeguards"} or any(not isinstance(probe[key], dict) for key in probe):
        raise GuardError("code_and_config", "invalid_shape")
    if (set(probe["hashes"]) != set(EXPECTED_HASHES)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in probe["hashes"].values())):
        raise GuardError("code_and_config", "invalid_shape")
    profile_name = next((name for name, profile in TRANSPORT_PROFILES.items()
                         if probe["hashes"] == profile["hashes"]
                         and probe["dependencies"] == profile["dependencies"]
                         and all(type(probe["dependencies"].get(key)) is type(value)
                                 for key, value in profile["dependencies"].items())), None)
    if profile_name is None:
        # Report bounded logical names, never raw environment or child output.
        closest = min(TRANSPORT_PROFILES.values(), key=lambda profile:
                      sum(probe["hashes"][key] != value for key, value in profile["hashes"].items()))
        mismatches = [key for key, value in closest["hashes"].items() if probe["hashes"][key] != value]
        mismatches.extend("dependency:" + key for key, value in closest["dependencies"].items()
                          if probe["dependencies"].get(key) != value
                          or type(probe["dependencies"].get(key)) is not type(value))
        raise GuardError("code_and_config", "unknown_transport_profile", mismatches)
    tree_digest = TRANSPORT_PROFILES[profile_name].get("undici_lib_sha256")
    if tree_digest is not None:
        tree = run_json(["docker", "exec", identity, "node", "-e", undici_tree_probe_source()],
                        "code_and_config", deadline)
        if (set(tree) != {"undici_lib_sha256", "file_count"}
                or type(tree.get("file_count")) is not int
                or not 0 < tree["file_count"] <= 2048):
            raise GuardError("code_and_config", "invalid_shape")
        if tree.get("undici_lib_sha256") != tree_digest:
            raise GuardError("code_and_config", "dependency_tree_mismatch", ["undici/lib"])
    for key, expected in (("predicates", EXPECTED_PREDICATES),):
        if set(probe[key]) != set(expected):
            raise GuardError("code_and_config", "invalid_shape")
        mismatches = [name for name, value in expected.items()
                      if probe[key][name] != value or type(probe[key][name]) is not type(value)]
        if mismatches:
            raise GuardError("code_and_config", key + "_mismatch", mismatches)
    safeguards = {"private_bypass_absent_or_empty", "db_auth_canonical_false",
                  "dotenv_absent", "alternate_dotenv_unconfigured", "expected_working_directory",
                  "node_options_unconfigured"}
    if set(probe["safeguards"]) != safeguards:
        raise GuardError("code_and_config", "invalid_shape")
    mismatches = [name for name in sorted(safeguards) if probe["safeguards"][name] is not True]
    if mismatches:
        raise GuardError("code_and_config", "safeguards_mismatch", mismatches)
    after = run_json(arguments, "container_after", deadline)
    if verify_container(after, "container_after") != identity or after["image"] != before["image"]:
        raise GuardError("container_after", "container_changed")
    return {"verified": True, "scope": "audited_firecrawl_transport_only",
            "receipt_sha256": TRANSPORT_PROFILES[profile_name]["receipt_sha256"],
            "transport_profile": profile_name, "api_image": before["image"],
            "container_id": identity, "compiled_hashes_checked": len(EXPECTED_HASHES),
            "configuration_predicates_checked": len(EXPECTED_PREDICATES),
            "extra_safeguards_checked": len(safeguards), "network_probes": 0,
            "runtime_mutations": False}


def verify():
    """Return a safe result dict; no output, exceptions, probes, or mutations."""
    try:
        return _preflight()
    except GuardError as error:
        return {"verified": False, "stage": error.stage, "error": error.code,
                "mismatches": error.mismatches}
    except Exception:
        return {"verified": False, "error": "unexpected_failure"}


def main():
    result = verify()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("verified") is True else 1


if __name__ == "__main__":
    sys.exit(main())
