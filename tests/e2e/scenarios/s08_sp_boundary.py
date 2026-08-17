#!/usr/bin/env python3
"""SCENARIO S08 — the DEPLOYER permission boundary. The deployer SP deploys CODE (bundle deploy) but must
NOT be able to do account-admin things — specifically, it must not be able to MANAGE the AIA role group
(add members). Confirms the identity separation the design relies on: the deployer (a synthesized non-admin
SP in dev; the CI SP in cicd) is not an admin, so it can't grant itself or the app SP into the role and
thereby inherit AIA's data access.

  (a) positive: the deployer CAN deploy — the app exists (proven if the deploy phase succeeded).
  (b) negative: the deployer holds NO group.manager (admin authority) on the role group.

This runs in BOTH strategies — the deployer identity differs (synth SP vs CI SP) but the boundary is the same.

    python3 -m tests.e2e.scenarios.s08_sp_boundary
"""
import urllib.parse

from tests.harness import applifecycle, config, dbx, report


def main(mode: str, restart) -> None:   # mode + restart unused — kept for the uniform run_all call signature
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    deployer_sp = cfg.require("DEPLOYER_SP")   # the synth deployer (dev) / the CI SP (cicd)
    app_name = cfg.require("APP_NAME")
    group_id = cfg.require("ASSUME_GROUP")     # the AIA role group's account id (written back by admin_prereqs)
    account_id = cfg.get("ACCOUNT_ID") or dbx.account_id(admin)
    r = report.Results()

    report.step("S08: confirm the deployer SP is a NON-admin deployer")
    r.check("app exists (the deploy worked)", bool(dbx.app(admin, app_name).get("name")))

    report.step("assert the deployer SP has no admin authority over the role group")
    w = dbx.client(admin)   # only an admin can READ the group's rule-set
    name = f"accounts/{account_id}/groups/{group_id}/ruleSets/default"
    rs = w.api_client.do("GET",
                         f"/api/2.0/preview/accounts/access-control/rule-sets"
                         f"?name={urllib.parse.quote(name)}&etag=")
    is_manager = any(
        rule.get("role") == "roles/group.manager"
        and f"servicePrincipals/{deployer_sp}" in (rule.get("principals") or [])
        for rule in (rs.get("grant_rules") or []))
    r.assert_eq("deployer SP is NOT a manager of the role group", False, is_manager)
    print("  (managing the role group — adding members — is ADMIN-only, never the deployer — separation holds)")
    r.finish()


if __name__ == "__main__":
    main("in_process", applifecycle.make_restart(config.load()))
