"""demo_substrate — the PoC's threat-intel SUBSTRATE table registry (DEMO-SPECIFIC).

These are the analytic tables the 5 UC-function tools query (accounts, indicators, telemetry, ...). They
come from the workshop dataset and exist ONLY so the PoC's tools return real results against seeded data.

*** AIA REPLACES THIS. *** In production the substrate is AIA's own detection/SIEM/threat-intel tables,
which have different names and schemas. This registry is deliberately its OWN module (not in common.py)
so swapping it out is a single-file change: point the tools in build_structure at your real tables and
drop this file + the seed CSVs. common.py stays put (it's the generic Ctx/naming layer, not data).

Each entry: (csv_file_basename, table_name, [(column, type_name), ...]). `type_name` is a plain string so
this stays importable without pyspark; spark_schema() maps it to a pyspark type at load time. Two tables
are renamed vs their CSV to avoid clashing with OUR domain tables:
  investigations.csv -> intel_investigations   (ours is `investigations`)
  incidents.csv      -> telemetry              (raw detection telemetry, not the case queue)
"""
SUBSTRATE_TABLES = [
    ("threat_actors", "threat_actors", [
        ("actor_id", "string"), ("actor_name", "string"), ("aliases", "string"),
        ("sophistication", "string"), ("motivation", "string"), ("origin_region", "string"),
        ("first_seen", "date"), ("last_seen", "date"), ("is_active", "boolean")]),
    ("campaigns", "campaigns", [
        ("campaign_id", "string"), ("campaign_name", "string"), ("actor_id", "string"),
        ("target_sector", "string"), ("mitre_ttps", "string"), ("severity", "string"),
        ("status", "string"), ("start_date", "date"), ("end_date", "date")]),
    ("indicators", "indicators", [
        ("indicator_id", "string"), ("indicator_value", "string"), ("indicator_type", "string"),
        ("campaign_id", "string"), ("confidence", "int"), ("first_seen", "date"),
        ("last_seen", "date"), ("source", "string"), ("is_active", "boolean"),
        ("times_seen", "int"), ("notes", "string")]),
    ("indicator_intel", "indicator_intel", [
        ("indicator_id", "string"), ("indicator_value", "string"), ("host", "string"),
        ("url_status", "string"), ("threat", "string"), ("tags", "string"),
        ("family", "string"), ("payload_md5", "string"), ("payload_sha256", "string"),
        ("urlhaus_reference", "string"), ("urlhaus_type", "string")]),
    ("accounts", "accounts", [
        ("account_id", "string"), ("customer_name", "string"), ("email", "string"),
        ("segment", "string"), ("plan_tier", "string"), ("region", "string"),
        ("country", "string"), ("signup_date", "date"), ("status", "string")]),
    ("risk_signals", "risk_signals", [
        ("signal_id", "string"), ("account_id", "string"), ("signal_type", "string"),
        ("severity", "string"), ("detected_at", "timestamp"), ("details", "string")]),
    ("account_risk_scores", "account_risk_scores", [
        ("score_id", "string"), ("account_id", "string"), ("score_date", "date"),
        ("risk_score", "int"), ("risk_band", "string"), ("top_signal", "string"),
        ("model_version", "string")]),
    ("investigations", "intel_investigations", [
        ("investigation_id", "string"), ("account_id", "string"), ("opened_at", "timestamp"),
        ("closed_at", "timestamp"), ("analyst", "string"), ("status", "string"),
        ("summary", "string"), ("detailed_notes", "string"), ("scenario", "string")]),
    ("account_actions", "account_actions", [
        ("action_id", "string"), ("account_id", "string"), ("action_type", "string"),
        ("reason_summary", "string"), ("taken_by", "string"), ("taken_at", "timestamp"),
        ("related_investigation_id", "string")]),
    ("incidents", "telemetry", [
        ("incident_id", "string"), ("created_at", "timestamp"), ("narrative", "string"),
        ("indicator_value", "string"), ("indicator_type", "string"), ("account_id", "string"),
        ("status", "string"), ("scenario_label", "string")]),
]


def spark_schema(spark, columns):
    """Turn a registry column list [(name, type_name)] into a pyspark StructType."""
    from pyspark.sql.types import (StructType, StructField, StringType, IntegerType,
                                   BooleanType, DateType, TimestampType)
    mapping = {"string": StringType(), "int": IntegerType(), "boolean": BooleanType(),
               "date": DateType(), "timestamp": TimestampType()}
    return StructType([StructField(n, mapping[t]) for n, t in columns])
