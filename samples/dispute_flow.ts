/**
 * dispute_flow.ts — reject -> dispute -> on-chain AI adjudication on v2.
 *
 * Uses separate sponsor and researcher signers. The dispute must be opened
 * inside the 3-day challenge window stamped by reject_claim. Both refund
 * paths are blocked during that window by the deployed v2 contract.
 *
 *   SPONSOR_PRIVATE_KEY=0x... RESEARCHER_PRIVATE_KEY=0x... \
 *   VAULT_ADDRESS=0xD57A790cDc4A6a456A2C064D80153B0cb679d724 \
 *   REPORT_URL=https://your.public/report \
 *     npx tsx samples/dispute_flow.ts
 */
import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import "dotenv/config";

function required(name: string): `0x${string}` {
  const value = process.env[name];
  if (!value?.startsWith("0x")) throw new Error(`${name} is required`);
  return value as `0x${string}`;
}

function decode<T>(value: unknown): T {
  let out = value;
  while (typeof out === "string") out = JSON.parse(out);
  return out as T;
}

const sponsorAccount = createAccount(required("SPONSOR_PRIVATE_KEY"));
const researcherAccount = createAccount(required("RESEARCHER_PRIVATE_KEY"));
const sponsor = createClient({ chain: studionet, account: sponsorAccount });
const researcher = createClient({ chain: studionet, account: researcherAccount });

const VAULT = required("VAULT_ADDRESS");
const REPORT_URL = process.env.REPORT_URL;
if (!REPORT_URL?.startsWith("https://")) throw new Error("REPORT_URL must be a public HTTPS URL");

const BOND = 20_000_000_000_000_000n; // 0.02 GEN
const ESCROW = 1_000_000_000_000_000_000n; // 1 GEN
const SCOPE =
  "Admin dashboard of app.example is in scope: auth bypass, IDOR, " +
  "stored XSS and privilege escalation chains on staging data.";
const POLICY = "Severity MEDIUM and above is payable";

async function main() {
  const config = decode<{ challenge_window_seconds?: number }>(
    await sponsor.readContract({ address: VAULT, functionName: "get_config", args: [] }),
  );
  if (config.challenge_window_seconds !== 259_200) {
    throw new Error("VAULT_ADDRESS is not the corrected v2 build");
  }

  await sponsor.writeContract({
    address: VAULT, functionName: "create_bounty",
    args: ["Arbitration sample", SCOPE, POLICY], value: ESCROW,
  });
  const bounties = decode<{ bounties: Array<{ bounty_id: number }> }>(
    await sponsor.readContract({
      address: VAULT, functionName: "get_bounties_by_sponsor",
      args: [sponsorAccount.address, 0, 1],
    }),
  );
  const bountyId = bounties.bounties[0].bounty_id;

  await researcher.writeContract({
    address: VAULT, functionName: "submit_claim",
    args: [bountyId, REPORT_URL, "working PoC with reproducible steps"],
  });
  const claims = decode<{ claims: Array<{ claim_id: number }> }>(
    await sponsor.readContract({
      address: VAULT, functionName: "get_claims_by_bounty", args: [bountyId, 0, 1],
    }),
  );
  const claimId = claims.claims[0].claim_id;

  await sponsor.writeContract({
    address: VAULT, functionName: "reject_claim", args: [claimId, "not exploitable"],
  });
  const rejected = decode<{
    challengeable: boolean;
    challenge_deadline_epoch: number;
  }>(await researcher.readContract({
    address: VAULT, functionName: "get_claim", args: [claimId],
  }));
  if (!rejected.challengeable) throw new Error("claim is not challengeable");
  console.log("challenge deadline:", rejected.challenge_deadline_epoch);

  // Optional consensus preview: no bond, no mutation.
  console.log("preview:", await researcher.writeContract({
    address: VAULT, functionName: "preview_arbitration", args: [claimId],
  }));

  console.log("arbitration:", await researcher.writeContract({
    address: VAULT, functionName: "dispute_claim", args: [claimId], value: BOND,
  }));
}

main().catch((err) => { console.error(err); process.exit(1); });