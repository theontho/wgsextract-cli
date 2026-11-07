#!/usr/bin/env node
/**
 * Loads each bundle the way the app does and reports whether it is valid: manifest and page rules
 * (docs/schema), setup tool version files, and per-platform script folders. Accepts bundle folders,
 * manifest.json files, and bundle archives. Exits 1 if any bundle is invalid.
 *
 * Usage: bundle-validate-cli.js [--quiet] <bundle>...
 */
import { loadManifestFromRoot, resolveBundleSourceRoot } from "./bundle-loader.js";
import { errorMessage } from "./errors.js";

const USAGE = "Usage: bundle-validate-cli.js [--quiet] <bundle>...";

async function validateBundles(bundles: readonly string[], log: (line: string) => void = console.log): Promise<number> {
    let failures = 0;
    for (const bundle of bundles) {
        try {
            const manifest = await loadManifestFromRoot(await resolveBundleSourceRoot(bundle));
            log(`OK   ${bundle} (${manifest.id})`);
        }
        catch (error) {
            failures += 1;
            console.error(`FAIL ${bundle}: ${errorMessage(error)}`);
        }
    }
    return failures === 0 ? 0 : 1;
}

async function main(argv: string[]): Promise<number> {
    const quiet = argv.includes("--quiet");
    const bundles = argv.filter((arg) => arg !== "--quiet");
    if (bundles.length === 0 || bundles.some((arg) => arg.startsWith("--"))) {
        console.error(USAGE);
        return 2;
    }
    return validateBundles(bundles, quiet ? () => undefined : console.log);
}

process.exitCode = await main(process.argv.slice(2));
