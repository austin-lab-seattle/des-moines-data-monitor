"""Immutable chronological chunks for exact, bounded observation page reads."""
import gzip
import hashlib
import io
import json

CHUNK_BYTES = 512 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def manifest_key(instrument, version):
    return f"_observations/v1/{instrument}/{version}/index.json"


def read_json(api, key, limit, digest=None):
    body = api.s3_client.get_object(Bucket=api.BUCKET, Key=key)["Body"]
    try:
        raw = body.read(limit + 1)
    finally:
        body.close()
    if len(raw) > limit or (digest and hashlib.sha256(raw).hexdigest() != digest):
        raise ValueError("Invalid observation index object")
    if digest:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Observation chunk is too large")
    return json.loads(raw)


def publish(api, instrument, text, etag, *, defer_manifest=False):
    # One paginated metadata listing replaces hundreds of redundant PUTs.
    # Only reuse complete, single-upload blobs with the expected S3 checksum.
    existing = {item['Key']: item for item in api.iter_s3_objects(
        f"_observations/v1/{instrument}/chunks/")}
    lines = (line for line in text.splitlines() if line.strip())
    header = next(lines, "")
    columns = api.split_fields(header) if header else []
    timestamp_index = api.column_index(columns)
    ordered = []
    for source_index, raw in enumerate(lines):
        fields = api.split_fields(raw)
        if len(fields) != len(columns):
            continue
        moment = api.datetime_for_record(instrument, columns, fields, timestamp_index)
        if moment is not None:
            ordered.append((moment.replace(tzinfo=api.timezone.utc).isoformat(), source_index, raw))
    ordered.sort(key=lambda row: (row[0], row[1]))
    chunks, current, size = [], [], 0

    def flush():
        raw = json.dumps(current, ensure_ascii=False).encode()
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("Observation chunk exceeds the reader limit")
        blob = gzip.compress(raw, mtime=0)
        # Python 3.11 and newer runtimes differ in gzip's OS header byte.
        # Normalize it so identical chunks retain their identity across hosts.
        blob = blob[:9] + b'\xff' + blob[10:]
        digest = hashlib.sha256(blob).hexdigest()
        key = f"_observations/v1/{instrument}/chunks/{digest}.json.gz"
        previous = existing.get(key, {})
        checksum = '"' + hashlib.md5(blob, usedforsecurity=False).hexdigest() + '"'
        if previous.get('Size') != len(blob) or previous.get('ETag') != checksum:
            api.s3_client.put_object(Bucket=api.BUCKET, Key=key, Body=blob,
                                     ContentType="application/gzip")
        chunks.append({"key": key, "digest": digest, "count": len(current),
                       "first": current[0][0], "last": current[-1][0]})

    for row in ordered:
        row_size = len(json.dumps(row, ensure_ascii=False).encode()) + 2
        if current and (size + row_size > CHUNK_BYTES or len(current) >= 1024):
            flush()
            current, size = [], 0
        current.append(row)
        size += row_size
    if current:
        flush()
    manifest = {"version": api.SILVER_VIEW_CODE_VERSION, "instrument_id": instrument,
                "silver_etag": etag, "columns": columns, "chunks": chunks}
    if not defer_manifest:
        commit(api, instrument, manifest, etag)
    return manifest


def commit(api, instrument, manifest, etag):
    raw = json.dumps({**manifest, "silver_etag": etag}).encode()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Observation manifest exceeds the reader limit")
    # Publish only after every immutable chunk has been uploaded successfully.
    api.s3_client.put_object(Bucket=api.BUCKET, Key=manifest_key(instrument, api.SILVER_VIEW_CODE_VERSION), Body=raw,
                             ContentType="application/json", CacheControl="no-store")


def read(api, event):
    params = api.query_params(event)
    instrument = params.get("instrument") or params.get("instrument_id")
    if not api.is_valid_instrument(instrument):
        return api.response(400, {"error": "Invalid or missing instrument"})
    try:
        order = params.get("order", "asc")
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        limit = min(max(int(params.get("limit", 100) or 100), 1), 500)
        cursor = max(int(params.get("cursor", 0) or 0), 0)
        start = api.datetime_for_compare(params.get("start"))
        end = api.datetime_for_compare(params.get("end"))
        api.validate_time_range(params.get("start"), params.get("end"), start, end)
    except (TypeError, ValueError) as exc:
        return api.response(400, {"error": str(exc)})
    try:
        etag = api.s3_client.head_object(Bucket=api.BUCKET, Key=f"{instrument}/silver/{instrument}_data.csv")["ETag"]
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return api.response(404, {"error": "Records not found", "instrument_id": instrument})
        return unavailable(api)
    try:
        manifest = read_json(api, manifest_key(instrument, api.SILVER_VIEW_CODE_VERSION), MAX_MANIFEST_BYTES)
        if (manifest["version"] != api.SILVER_VIEW_CODE_VERSION or manifest["silver_etag"] != etag
                or manifest["instrument_id"] != instrument):
            return unavailable(api)
        columns = manifest["columns"]
        timestamp_index = api.column_index(columns)
        chunks = manifest["chunks"] if order == "asc" else reversed(manifest["chunks"])
        count_only = str(params.get("count_only", "")).lower() in {"1", "true", "yes"}
        skipped, count, selected = 0, 0, []
        for chunk in chunks:
            first, last = api.datetime_for_compare(chunk["first"]), api.datetime_for_compare(chunk["last"])
            if (start is not None and last < start) or (end is not None and first > end):
                continue
            whole = (start is None or first >= start) and (end is None or last <= end)
            if whole and count_only:
                count += chunk["count"]
                continue
            if whole and skipped + chunk["count"] <= cursor and not count_only:
                skipped += chunk["count"]
                continue
            expected_key = f"_observations/v1/{instrument}/chunks/{chunk['digest']}.json.gz"
            if chunk["key"] != expected_key:
                raise ValueError("Invalid observation chunk identity")
            records = read_json(api, expected_key, MAX_BODY_BYTES, chunk["digest"])
            if len(records) != chunk["count"]:
                raise ValueError("Invalid observation chunk length")
            if order == "desc":
                records.reverse()
            for stamp, source_index, raw in records:
                moment = api.datetime_for_compare(stamp)
                if not api.row_matches_time_range(moment, start, end):
                    continue
                if count_only:
                    count += 1
                elif skipped < cursor:
                    skipped += 1
                else:
                    selected.append((moment, source_index, raw))
                    if len(selected) > limit:
                        break
            if not count_only and len(selected) > limit:
                break
        if count_only:
            return api._view_response(api.response(200, {"instrument_id": instrument, "count": count}), "indexed")
        # Always join current annotations. Review edits never trigger a Silver scan.
        flags = api.read_review_objects(instrument, "flags", strict=True)
        corrections = api.read_review_objects(instrument, "corrections", strict=True)
        rows = []
        for moment, source_index, raw in selected[:limit]:
            fields = api.split_fields(raw)
            stamp = api.record_timestamp(instrument, columns, fields, timestamp_index)
            rows.append(api.annotated_silver_record(instrument, columns, fields, source_index, raw,
                                                    stamp, moment, flags, corrections))
        return api._view_response(api.response(200, {"instrument_id": instrument, "columns": columns,
            "rows": rows, "next_cursor": cursor + len(rows) if len(selected) > limit else None,
            "review_object_count": len(flags) + len(corrections)}), "indexed")
    except Exception as exc:
        print(f"Observation index unavailable for {instrument}: {type(exc).__name__}")
        return unavailable(api)


def unavailable(api):
    return api.response(503, {"error": "Readings are being prepared. Please retry shortly."}, {"Retry-After": "5"})
