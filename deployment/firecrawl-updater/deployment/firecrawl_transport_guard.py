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
# Frozen 683 public-fetch audit: deployment/firecrawl-transport-20261105-v683.json.
# Complete profile only; package admission and Owner approval remain separate.
TRANSPORT_PROFILES["v2.11.683"] = {
    "receipt_sha256": "9b301476c32355e1ec6fbe1808552e2667516d0740f327eeda7bd5de4a30e853",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.629"]["hashes"],
        "/app/dist/src/scraper/scrapeURL/engines/index.js": "a701dc79f1be22db0fc7c4d962ef34abbb5f467ea66f738df82303751ca7296f",
        "/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js": "25944a8f4bcb2986ca9a548df0d8d0fa1476b7c7745465b63f4b599b3baeeb5c",
        "/app/dist/src/scraper/scrapeURL/engines/utils/downloadFile.js": "8ac395b87365de206b5a585f41be070c3b5cb9fa5f403c6613d19e5bc119e746",
        "/app/dist/src/scraper/scrapeURL/engines/fetch/index.js": "6500fe7f20d148650a42d11f0b85554b6581016f3c386b71d36f4f3a27a8fd2a",
        "/app/dist/src/scraper/scrapeURL/engines/pdf/index.js": "23a5a3510e1e6cbbad7f96f65c73c2fff8dc604ddc79bebeb7c0e9e503ab3002",
        "/app/dist/src/scraper/scrapeURL/index.js": "70fd1b3a607a8367c038df75d5d306514063b62adcd6f2fa99a8f6ec61745e9a",
        "/app/dist/src/controllers/v2/types.js": "a5bb9e0d2974b4f60488a477e8e73e19d89170d84f7c0ef67a51acfb9e7ccfaa",
        "/app/dist/src/lib/exchange.js": "048ce694b30a201d5c38e6fd18f8b47cead91e8d6fbbf55925462865d5c276bd",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.1", "ipaddr_js": "2.2.0"},
    "undici_lib_sha256": "213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c",
}
# Frozen 704 public-fetch audit: deployment/firecrawl-transport-20261109-v704.json.
# v2.11.705 compiles to the same guarded bytes. Package admission and Owner approval remain separate.
TRANSPORT_PROFILES["v2.11.704"] = {
    "receipt_sha256": "ca53972c4546835b96f663a2a8b87f9a1b252d88853b455dd429802593d20bcc",
    "hashes": {
        **TRANSPORT_PROFILES["v2.11.683"]["hashes"],
        "/app/dist/src/scraper/scrapeURL/index.js": "53f75c638e1d88721d25d6b3c52277da11b66809a3c720751fb4b57202632b6e",
        "/app/dist/src/lib/exchange.js": "8e96b46a2c38a9babec74335112eaeaceaa850576aedf6f60b03541a029ca984",
    },
    "dependencies": {"node_major": 22, "undici": "7.30.1", "ipaddr_js": "2.2.0"},
    "undici_lib_sha256": "213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c",
}
# Frozen audit: deployment/firecrawl-transport-20261113-v735.json.
# Complete syntax program and dependency trees; future stable tags may reuse it.
TRANSPORT_PROFILES["v2.11.735"] = {'receipt_sha256': '1356f8cc01f340d86d15e62770d764cb0583e845f614291a5163b96d2b2d27ba',
 'hashes': {'/app/dist/src/scraper/scrapeURL/engines/index.js': 'ff98b31019085de4a21ec2b3b8a4bed788db9de1a790f877d08b4a9d97e22774',
            '/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js': '25944a8f4bcb2986ca9a548df0d8d0fa1476b7c7745465b63f4b599b3baeeb5c',
            '/app/dist/src/scraper/scrapeURL/engines/utils/downloadFile.js': '8ac395b87365de206b5a585f41be070c3b5cb9fa5f403c6613d19e5bc119e746',
            '/app/dist/src/scraper/scrapeURL/engines/fetch/index.js': '6500fe7f20d148650a42d11f0b85554b6581016f3c386b71d36f4f3a27a8fd2a',
            '/app/dist/src/scraper/scrapeURL/engines/pdf/index.js': '6a08e5c949292c9c65793baf15bbc60216764f37ab456f919e528f311338d76b',
            '/app/dist/src/scraper/scrapeURL/engines/document/index.js': '9ee8d7745dbc40ae9715338feecb15adc3bff488818b667929ace8350059bc62',
            '/app/dist/src/scraper/scrapeURL/index.js': '8904a5826c4b494ef407657063f8271821efc2887b764521a178e52419a36dfb',
            '/app/dist/src/controllers/v2/types.js': '28d504d246bfbf849f48649b18c325019ce1650a3d066a5dd64a2b2a245ac2d7',
            '/app/dist/src/lib/exchange.js': '28ba3c79d6390ca68f556cac3e54353b20ced0a8556725e7ae406eb4ab354ac7',
            'undici/client.js': '84205dc96cb9bf7432c4b3e80f0babaa11d6f4faf24ead3e7d9660e9d8fee0b3',
            'ipaddr.js': '75c602c8834cc9e1bcdb7d1d2eaadbbd5ba0e19660d9ea3f450dbf17f2ec8495'},
 'dependencies': {'node_major': 22, 'undici': '7.30.1', 'ipaddr_js': '2.2.0'},
 'undici_lib_sha256': '213bda9bf7c6860b6714484b606b431c7bdbe4652a20be22e3e20e8b96e1187c',
 'just_bash_tree_sha256': 'ff1f7ad0597080c3a3bb3cf5a752db4527de621a12dcee0f4faa0e6500b7aa9b',
 'syntax_hashes': {'/app/dist/src/scraper/scrapeURL/engines/index.js': '6ea558b99c4aa8c750cd36b184f43be7e912c929a0d1d7446e46d1d05b68ebb1',
                   '/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js': 'e0fe001c4f52e47f7a6551095e17726a4cce94f4cb093e35d878b239b66a4033',
                   '/app/dist/src/scraper/scrapeURL/engines/utils/downloadFile.js': 'df4757bb63aa7c2bcf50a3485ed2a45bb55b4894c352e6c562347de46457248b',
                   '/app/dist/src/scraper/scrapeURL/engines/fetch/index.js': 'c56db8903bb161e6b2950971168618d57ebe653760836558457b6da828bb19eb',
                   '/app/dist/src/scraper/scrapeURL/engines/pdf/index.js': 'c91e5fe4712f33c3cb741d405ae10f53118eab63bd8078c44c6c87d18485667f',
                   '/app/dist/src/scraper/scrapeURL/engines/document/index.js': '86e17f11fff187fec395e52d659bc506c3dd67bed76847f6adeb3cbf08c42e19',
                   '/app/dist/src/scraper/scrapeURL/index.js': '36e6c60ab39e8bbe454f584d9d63aa2ba3e5f45cd6a998c0accb306b70efdde4',
                   '/app/dist/src/controllers/v2/types.js': '428add5a75053ae6680371111a939a7345f25890803f1bb331533813abc031f9',
                   '/app/dist/src/lib/exchange.js': '43cf8cf01beb14ec73395ec5f61ae4cdcfc702c2ce77369cea2857635d771dd5',
                   'undici/client.js': '7e7cb53c1ef1483358468fbe91463a836b5b6e5fe2e791fc509417735d602646',
                   'ipaddr.js': 'c654870b883b9d766ecc880a28899f19a5a68f1b4e9c6ee8777fe62c62222af0',
                   '/app/dist/src/config.js': 'da0aed61708934427d7202e642c75d20adbabaecbcd1978d12a02d5a5086659b',
                   '/app/dist/src/index.js': '76a56926c8d7c4b41b556b6b9195484a19b2babbd1bdc406cbd75b157825fa46',
                   '/app/dist/src/controllers/v2/bash.js': 'beb619eeddd3a12997a3ffa3e45195cc19be8af8cd9e6c8ef0ab408821c27920',
                   '/app/dist/src/lib/reserved-capacity.js': '7b95c4334e83e2a1d2a92c6f4fdbecaabf2e122400d2998167965c7d677337e8',
                   '/app/dist/src/lib/x-twitter-url.js': 'c6ff2d615bfa1bc5d643fa555cfb836ef07ee3ec2734ec0768ac8bd83b8be212',
                   '/app/dist/src/lib/hosted-bash/docs.js': '25d1d63a1edd695d68f6e7c4b0b58f29e6a3521da380012e993c698a178164eb',
                   '/app/dist/src/lib/hosted-bash/firecrawl-command.js': '9baf6787e09e83eb6ff6dd7297622c6c8f67c7abd9aee9c9e6430ee2924b0d90',
                   '/app/dist/src/lib/hosted-bash/shell-worker.js': '31d44512c9ef6f54f0c25515021c7c042268557c20a0e53f72eb510c9d76e869',
                   '/app/dist/src/lib/hosted-bash/shell.js': '571bd0790c3cc4488a4bc9d38ec66b27fa622992789e72a4cac0de59f98dbd64',
                   '/app/dist/src/lib/hosted-bash/limits.js': 'cea83ff6969ea4a86d66485acec48b468aa154cc317d42c340667fe03c82156a',
                   '/app/dist/src/scraper/scrapeURL/error.js': 'efe18bc09ad6ef98015dd2c78e48c7892a433e306204f84867ecc51a57a31eb7'}}

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
  const workerLoad = require('module').createRequire('/app/dist/src/lib/hosted-bash/shell-worker.js');
  let shellPackage = false;
  try { workerLoad.resolve('just-bash'); shellPackage = true; }
  catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  const hosted_bash_present = shellPackage || fs.existsSync('/app/dist/src/lib/hosted-bash')
    || fs.existsSync('/app/dist/src/controllers/v2/bash.js');
  console.log(JSON.stringify({hashes, dependencies, predicates: actual, safeguards, hosted_bash_present}));
} catch (_) {
  process.exitCode = 1;
}
""".replace("PATHS", json.dumps(list(EXPECTED_HASHES))).replace("PREDICATES", json.dumps(EXPECTED_PREDICATES))


def syntax_normalizer_source():
    # Use Node's bundled parser, never a package supplied by the candidate.
    # Keep directives, ASI, regexp flags and tagged-template raw text. Only
    # source positions and Literal.raw (an alternate spelling) are discarded.
    return """
const crypto = require('node:crypto');
const bundled = process.binding('natives')['internal/deps/acorn/acorn/dist/acorn'];
if (typeof bundled !== 'string') throw new Error('Bundled parser unavailable');
const parser = {exports: {}};
require('node:vm').runInThisContext('(function(exports,module){' + bundled + '\\n})',
  {filename: 'node-bundled-acorn'})(parser.exports, parser);
function syntaxHash(source) {
  const ast = parser.exports.parse(source, {ecmaVersion: 'latest', sourceType: 'script', allowHashBang: true});
  const canonical = JSON.stringify(ast, function(key, value) {
    if (key === 'start' || key === 'end' || key === 'loc' || key === 'range') return undefined;
    if (key === 'raw' && this.type === 'Literal') return undefined;
    if (typeof value === 'number' && !Number.isFinite(value)) return {number: String(value)};
    return typeof value === 'bigint' ? {bigint: value.toString()} : value;
  });
  return crypto.createHash('sha256').update(canonical).digest('hex');
}
"""


def syntax_probe_source(paths):
    return syntax_normalizer_source() + """
const fs = require('node:fs'), path = require('node:path');
const load = require('node:module').createRequire('/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js');
try {
  const syntax_hashes = {};
  for (const name of PATHS) {
    const resolved = name === 'undici/client.js'
      ? path.join(path.dirname(load.resolve('undici/package.json')), 'lib/dispatcher/client.js')
      : name === 'ipaddr.js' ? load.resolve('ipaddr.js') : name;
    const stat = fs.lstatSync(resolved);
    if (!stat.isFile() || stat.size > 1048576) throw new Error();
    syntax_hashes[name] = syntaxHash(fs.readFileSync(resolved, 'utf8'));
  }
  console.log(JSON.stringify({syntax_hashes}));
} catch (_) { process.exitCode = 1; }
""".replace('PATHS', json.dumps(list(paths)))


def undici_tree_probe_source(package="undici"):
    # Read bounded static trees only; reject links and special files.
    if package == "undici":
        root = "path.join(path.dirname(load.resolve('undici/package.json')), 'lib')"
        key, max_file = "undici_lib_sha256", 1048576
    elif package == "just-bash":
        return just_bash_tree_probe_source()
    else:
        raise ValueError("Unsupported audited dependency")
    return """
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const load = require('module').createRequire('/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js');
try {
  const root = ROOT_EXPRESSION;
  if (!fs.lstatSync(root).isDirectory()) throw new Error();
  const entries = []; let total = 0;
  function walk(dir) {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name), stat = fs.lstatSync(full);
      if (stat.isSymbolicLink()) throw new Error();
      if (stat.isDirectory()) walk(full);
      else if (stat.isFile()) {
        total += stat.size;
        if (stat.size > MAX_FILE_BYTES || total > 33554432) throw new Error();
        const digest = crypto.createHash('sha256').update(fs.readFileSync(full)).digest('hex');
        entries.push([path.relative(root, full).split(path.sep).join('/'), digest]);
        if (entries.length > 2048) throw new Error();
      } else throw new Error();
    }
  }
  walk(root);
  entries.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  const digest = crypto.createHash('sha256').update(JSON.stringify(entries)).digest('hex');
  console.log(JSON.stringify({[TREE_KEY]: digest, file_count: entries.length}));
} catch (_) { process.exitCode = 1; }
""".replace('ROOT_EXPRESSION', root).replace('MAX_FILE_BYTES', str(max_file)).replace('TREE_KEY', json.dumps(key))


def just_bash_tree_probe_source():
    # Follow package resolution from the actual worker. Hash each package and
    # its declared dependency/peer graph without importing candidate modules.
    return r"""
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const createRequire = require('node:module').createRequire;
const worker = '/app/dist/src/lib/hosted-bash/shell-worker.js';
try {
  const nodes = [], seen = new Map(); let count = 0, total = 0;
  function locate(load, name) {
    for (const base of load.resolve.paths(name) || []) {
      const manifest = path.join(base, name, 'package.json');
      if (fs.existsSync(manifest)) {
        const root = fs.realpathSync(path.dirname(manifest));
        const info = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
        if (info.name !== name) throw new Error('Package identity');
        return {root, info};
      }
    }
    return null;
  }
  function visit(pkg) {
    if (seen.has(pkg.root)) return seen.get(pkg.root);
    if (nodes.length >= 128) throw new Error('Package limit');
    const index = nodes.length; seen.set(pkg.root, index);
    const node = {name: pkg.info.name, version: pkg.info.version, files: [], dependencies: []};
    nodes.push(node);
    function walk(dir) {
      for (const name of fs.readdirSync(dir).sort()) {
        if (name === 'node_modules') continue; // Resolved and bound as graph edges below.
        const full = path.join(dir, name), stat = fs.lstatSync(full);
        if (stat.isSymbolicLink()) throw new Error('Linked package content');
        if (stat.isDirectory()) walk(full);
        else if (stat.isFile()) {
          total += stat.size;
          if (++count > 16384 || stat.size > 33554432 || total > 268435456) throw new Error('Size limit');
          node.files.push([path.relative(pkg.root, full).split(path.sep).join('/'),
            crypto.createHash('sha256').update(fs.readFileSync(full)).digest('hex')]);
        } else throw new Error('Special package content');
      }
    }
    walk(pkg.root);
    node.files.sort((a,b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
    const load = createRequire(path.join(pkg.root, 'package.json'));
    const required = pkg.info.dependencies || {}, optional = pkg.info.optionalDependencies || {};
    const peers = pkg.info.peerDependencies || {};
    for (const name of [...new Set([...Object.keys(required), ...Object.keys(optional), ...Object.keys(peers)])].sort()) {
      if (require('node:module').isBuiltin(name)) { node.dependencies.push([name, 'node:' + name]); continue; }
      const child = locate(load, name);
      if (!child && !(name in optional) && !(pkg.info.peerDependenciesMeta?.[name]?.optional))
        throw new Error('Missing dependency');
      node.dependencies.push([name, child ? visit(child) : null]);
    }
    return index;
  }
  const load = createRequire(worker), pkg = locate(load, 'just-bash');
  if (!pkg) throw new Error('Missing sandbox');
  const entry = fs.realpathSync(load.resolve('just-bash'));
  if (!entry.startsWith(pkg.root + path.sep)) throw new Error('Sandbox resolution mismatch');
  visit(pkg);
  const just_bash_tree_sha256 = crypto.createHash('sha256').update(JSON.stringify({
    entry: path.relative(pkg.root, entry).split(path.sep).join('/'), nodes})).digest('hex');
  console.log(JSON.stringify({just_bash_tree_sha256, file_count: count}));
} catch (_) { process.exitCode = 1; }
"""


def _preflight():
    deadline = time.monotonic() + TOTAL_TIMEOUT
    arguments = ["docker", "inspect", "--format", INSPECT_FORMAT, CONTAINER]
    before = run_json(arguments, "container_before", deadline)
    identity = verify_container(before, "container_before")
    # Pin the exec to the inspected container ID, not a potentially rebound name.
    probe = run_json(["docker", "exec", identity, "node", "-e", probe_source()], "code_and_config", deadline)
    if (set(probe) != {"hashes", "dependencies", "predicates", "safeguards", "hosted_bash_present"}
            or type(probe["hosted_bash_present"]) is not bool
            or any(not isinstance(probe[key], dict) for key in ("hashes", "dependencies", "predicates", "safeguards"))):
        raise GuardError("code_and_config", "invalid_shape")
    if (set(probe["hashes"]) != set(EXPECTED_HASHES)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in probe["hashes"].values())):
        raise GuardError("code_and_config", "invalid_shape")
    def dependencies_match(profile):
        return (probe["dependencies"] == profile["dependencies"]
                and all(type(probe["dependencies"].get(key)) is type(value)
                        for key, value in profile["dependencies"].items()))
    profile_name = next((name for name, profile in TRANSPORT_PROFILES.items()
                         if probe["hashes"] == profile["hashes"] and dependencies_match(profile)), None)
    if probe["hosted_bash_present"] and profile_name is not None:
        if not TRANSPORT_PROFILES[profile_name].get("just_bash_tree_sha256"):
            raise GuardError("code_and_config", "shell_profile_required")
    fingerprint_match, syntax_checked = "exact", 0
    # A complete audited syntax profile may also admit changed formatting or
    # comments. Never learn expectations from the candidate or mix profiles.
    syntax_candidates = [(name, profile) for name, profile in TRANSPORT_PROFILES.items()
                         if profile.get("syntax_hashes") and dependencies_match(profile)
                         and (not probe["hosted_bash_present"] or profile.get("just_bash_tree_sha256"))
                         and (profile_name is None or name == profile_name)]
    if syntax_candidates:
        paths = sorted({key for _, profile in syntax_candidates for key in profile["syntax_hashes"]})
        syntax = run_json(["docker", "exec", identity, "node", "-e", syntax_probe_source(paths)],
                          "code_and_config", deadline)
        if (set(syntax) != {"syntax_hashes"} or not isinstance(syntax["syntax_hashes"], dict)
                or set(syntax["syntax_hashes"]) != set(paths)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in syntax["syntax_hashes"].values())):
            raise GuardError("code_and_config", "invalid_syntax_shape")
        matched = next((name for name, profile in syntax_candidates
                        if all(syntax["syntax_hashes"][key] == value
                               for key, value in profile["syntax_hashes"].items())), None)
        if matched is None and profile_name is not None:
            raise GuardError("code_and_config", "audited_syntax_mismatch",
                             [key for key, value in TRANSPORT_PROFILES[profile_name]["syntax_hashes"].items()
                              if syntax["syntax_hashes"][key] != value])
        if matched is not None:
            fingerprint_match = "exact" if profile_name == matched else "syntax"
            profile_name = matched
            syntax_checked = len(TRANSPORT_PROFILES[matched]["syntax_hashes"])
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
    sandbox_digest = TRANSPORT_PROFILES[profile_name].get("just_bash_tree_sha256")
    if sandbox_digest is not None:
        tree = run_json(["docker", "exec", identity, "node", "-e", undici_tree_probe_source("just-bash")],
                        "code_and_config", deadline)
        if (set(tree) != {"just_bash_tree_sha256", "file_count"}
                or type(tree.get("file_count")) is not int
                or not 0 < tree["file_count"] <= 16384):
            raise GuardError("code_and_config", "invalid_shape")
        if tree.get("just_bash_tree_sha256") != sandbox_digest:
            raise GuardError("code_and_config", "dependency_tree_mismatch", ["just-bash"])
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
            "fingerprint_match": fingerprint_match, "syntax_hashes_checked": syntax_checked,
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
