# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Comprehensive test suite (121 tests covering sync logic, providers, utilities)
- Retry logic with exponential backoff for transient network failures
- Graceful degradation in watch mode - daemon never crashes from recoverable errors
- Health check logging every 10 sync cycles
- HTTP status codes in error messages for easier debugging

### Fixed
- JSON parsing errors no longer crash sync cycles (malformed responses handled gracefully)
- Provider factory errors now include supported providers list and configuration hints

### Changed
- Enhanced startup logging with DNS/proxy provider details and instance configuration
- Improved error context with URL and status code information
