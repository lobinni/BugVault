/**
 * deploy_and_fund.ts — deploy a fresh pair and run the direct-approval path.
 *
 * Sponsor and researcher MUST be different accounts: BugVault deliberately
 * rejects self-claims. The sponsor deploys and funds; the researcher submits;
 * the sponsor approves.
 *
 *   npm i genlayer-js dotenv
 *   SPONSOR_PRIVATE_KEY=0x... RESEARCHER_PRIVATE_KEY=0x... \
 *     npx tsx samples/deploy_and_fund.ts
 */
import { readFileSync } from "node:fs";
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

const ESCROW = 1_000_000_000_000_000_000n; // 1 GEN
const SCOPE =
  "Admin dashboard of app.example is in scope: auth bypass, IDOR, " +
  "stored XSS and privilege escalation chains on staging data.";
const POLICY = "Severity MEDIUM and above is payable";

async function main() {
  const vaultAddr = await sponsor.deployContract({
    code: readFileSync("contracts/bug_vault.py", "utf8"), args: [200],
  });
  const gateAddr = await sponsor.deployContract({
    code: readFileSync("contracts/cred_pass.py", "utf8"), args: [vaultAddr],
  });
  console.log("BugVault:", vaultAddr);
  console.log("CredPass:", gateAddr);

  await sponsor.writeContract({
    address: vaultAddr,
    functionName: "create_bounty",
    args: ["App.example Q3", SCOPE, POLICY],
    value: ESCROW,
  });

  const sponsorFeed = decode<{ bounties: Array<{ bounty_id: number }> }>(
    await sponsor.readContract({
      address: vaultAddr,
      functionName: "get_bounties_by_sponsor",
      args: [sponsorAccount.address, 0, 1],
    }),
  );
  const bountyId = sponsorFeed.bounties[0].bounty_id;

  await researcher.writeContract({
    address: vaultAddr,
    functionName: "submit_claim",
    args: [bountyId, "https://reports.example/idora-poc", "repro steps inside"],
  });

  const claimFeed = decode<{ claims: Array<{ claim_id: number }> }>(
    await sponsor.readContract({
      address: vaultAddr,
      functionName: "get_claims_by_bounty",
      args: [bountyId, 0, 1],
    }),
  );
  const claimId = claimFeed.claims[0].claim_id;

  await sponsor.writeContract({
    address: vaultAddr, functionName: "approve_claim", args: [claimId],
  });

  console.log("settled bounty:", await sponsor.readContract({
    address: vaultAddr, functionName: "get_bounty", args: [bountyId],
  }));
}

main().catch((err) => { console.error(err); process.exit(1); });