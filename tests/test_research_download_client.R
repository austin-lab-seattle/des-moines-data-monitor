# Offline integration test: real request construction and file verification.
source("scripts/research/download_data.R")
test_research_download <- function() {
  Sys.setenv(AQ_API_KEY = "CI-only-research-key")
  on.exit(Sys.unsetenv("AQ_API_KEY"), add = TRUE)
  directory <- tempfile()
  dir.create(directory)
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  blob <- memCompress(charToRaw("value\n-1.234567891\n"), "gzip")
  expected <- paste(openssl::sha256(blob), collapse = "")
  bad_checksum <- FALSE
  calls <- list()
  download <- function(url, destfile, ...) {
    stopifnot(!"headers" %in% names(list(...)), grepl("amazonaws.com/", url, fixed = TRUE))
    writeBin(blob, destfile)
    invisible(0)
  }
  transport <- function(req) {
    stopifnot(identical(req$headers[["x-api-key"]], "CI-only-research-key"),
              startsWith(req$url, "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/air-quality/v1/keyed/research/"),
              identical(req$options$followlocation, FALSE))
    calls[[length(calls) + 1L]] <<- req
    if (endsWith(req$url, "/download")) {
      result <- list(state = "READY", rows = 1L,
        url = "https://research.s3.us-west-2.amazonaws.com/export.csv.gz?temporary-signature",
        sha256 = if (bad_checksum) "corrupted" else expected)
    } else if (endsWith(req$url, "/exports")) {
      result <- list(job_id = "ci-job")
    } else result <- list(state = "READY")
    httr2::response_json(body = result)
  }
  output <- file.path(directory, "data.csv.gz")
  httr2::with_mocked_responses(transport, {
    path <- download_research_data("NO2-CAPS", include_flagged = TRUE, output = output, .download_file = download)
    stopifnot(identical(path, output), identical(readBin(output, "raw", n = length(blob)), blob))
    stopifnot(length(calls) == 3L, identical(calls[[1L]]$body$data$include_flagged, TRUE))
    bad_checksum <- TRUE
    failure <- tryCatch({
      download_research_data("NO2-CAPS", output = output, overwrite = TRUE, .download_file = download)
      NULL
    }, error = identity)
    stopifnot(inherits(failure, "error"), grepl("integrity", conditionMessage(failure)))
    stopifnot(identical(readBin(output, "raw", n = length(blob)), blob), length(list.files(directory)) == 1L)
  })
  cat("R key-only export flow, explicit flag toggle, credential isolation and checksum protection passed.\n")
}
test_research_download()
