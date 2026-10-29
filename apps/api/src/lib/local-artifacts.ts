import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { isAbsolute, resolve, sep } from "node:path";
import { createHash, randomUUID } from "node:crypto";

export function localJobArtifactKey(id: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) {
    throw new Error("Invalid local snapshot job ID");
  }
  const compact = id.replace(/-/g, "");
  const hashed = compact[12] === "7" && parseInt(compact.slice(0, 12), 16) >= Date.UTC(2026, 4, 26);
  const filename = hashed ? `${createHash("sha256").update(id).digest("hex")}-${id}` : id;
  return `jobs/${filename}.json`;
}

// Opt-in retained storage for isolated self-hosted deployments. No Cloud request.
export function localArtifactsEnabled(): boolean {
  return Boolean(process.env.SELF_HOSTED_ARTIFACT_DIR);
}

function artifactPath(key: string): string {
  const root = process.env.SELF_HOSTED_ARTIFACT_DIR;
  if (!root || !isAbsolute(root)) {
    throw new Error("SELF_HOSTED_ARTIFACT_DIR must be an absolute private directory");
  }
  if (!/^[a-zA-Z0-9][a-zA-Z0-9/._-]*$/.test(key) || key.split("/").some(x => x === ".." || x === ".")) {
    throw new Error("Invalid local artifact key");
  }
  const path = resolve(root, key);
  if (!path.startsWith(resolve(root) + sep)) throw new Error("Artifact escapes storage root");
  return path;
}

export async function writeLocalArtifact(key: string, value: unknown): Promise<void> {
  const path = artifactPath(key);
  await mkdir(resolve(path, ".."), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally {
    await rm(temporary, { force: true });
  }
}

export async function readLocalArtifact<T>(key: string): Promise<T | null> {
  try {
    return JSON.parse(await readFile(artifactPath(key), "utf8")) as T;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

export async function removeLocalArtifact(key: string): Promise<void> {
  await rm(artifactPath(key), { force: true });
}
