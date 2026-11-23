# source("scripts/research/download_data.R")
# Set AQ_API_KEY in your environment, then call download_research_data().
# Requires httr2 and openssl; never sends the API key to the download host.
download_research_data <- function(instrument, include_flagged = FALSE,
                                   start = NULL, end = NULL,
                                   output = paste0(instrument, "_research.csv.gz"),
                                   overwrite = FALSE, .download_file = utils::download.file) {
  stopifnot(instrument %in% c("BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"),
            is.logical(include_flagged), length(include_flagged) == 1L,
            !is.na(include_flagged))
  key <- Sys.getenv("AQ_API_KEY")
  if (!nzchar(key)) stop("Set AQ_API_KEY to your approved research key.")
  if (file.exists(output) && !overwrite) stop("Output exists; choose another path or set overwrite=TRUE.")
  base <- "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/air-quality/v1/keyed/research"
  api_request <- function(path, body = NULL) {
    req <- httr2::request(paste0(base, path)) |>
      httr2::req_headers("x-api-key" = key) |>
      httr2::req_options(followlocation = FALSE) |>
      httr2::req_timeout(30)
    if (!is.null(body)) req <- httr2::req_body_json(req, body, auto_unbox = TRUE)
    response <- httr2::req_perform(req)
    if (httr2::resp_status(response) != 200L && httr2::resp_status(response) != 202L)
      stop("Unexpected API response; no file downloaded.")
    httr2::resp_body_json(response, simplifyVector = TRUE)
  }
  body <- list(instrument = instrument, include_flagged = include_flagged)
  if (!is.null(start)) body$start <- start
  if (!is.null(end)) body$end <- end
  job <- api_request("/exports", body)
  deadline <- Sys.time() + 1200
  repeat {
    status <- api_request(paste0("/exports/", job$job_id))
    if (identical(status$state, "READY")) break
    if (!status$state %in% c("PENDING", "PROCESSING")) stop(paste("Export", status$state))
    if (Sys.time() >= deadline) stop("Export still preparing; check Your exports in Team Console.")
    Sys.sleep(3)
  }
  result <- api_request(paste0("/exports/", job$job_id, "/download"))
  if (!identical(result$state, "READY") || is.null(result$url)) stop("Prepare a new export.")
  if (!grepl("^https://[a-z0-9.-]+\\.s3\\.us-west-2\\.amazonaws\\.com/", result$url))
    stop("Unexpected download host.")
  temporary <- tempfile(tmpdir = dirname(output))
  on.exit(unlink(temporary), add = TRUE)
  # No authenticated API request or key header is used for the signed S3 URL.
  .download_file(result$url, temporary, method = "libcurl", mode = "wb", quiet = TRUE)
  connection <- file(temporary, "rb")
  checksum <- tryCatch(paste(openssl::sha256(connection), collapse = ""), finally = close(connection))
  if (!identical(checksum, result$sha256)) stop("Download integrity check failed.")
  if (!file.rename(temporary, output)) stop("Could not move the verified download into place.")
  message(format(result$rows, big.mark = ","), " rows saved to ", output)
  invisible(output)
}
