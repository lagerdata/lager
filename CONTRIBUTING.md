# Contributing to Lager

Thank you for your interest in contributing to Lager! This document provides guidelines and information for contributors.

## Getting Started

### Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/lagerdata/lager.git
   cd lager
   ```

2. **Install the CLI in development mode:**
   ```bash
   cd cli
   pip install -e .
   ```

3. **Verify installation:**
   ```bash
   lager --version
   ```

### Project Structure

```
lager/
├── cli/                    # Command-line interface
│   ├── commands/           # CLI command modules
│   ├── context/            # Context and session management
│   ├── impl/               # Box execution scripts
│   └── deployment/         # Deployment scripts (packaged with CLI)
├── box/                    # Box-side code
│   └── lager/              # Hardware control libraries
├── test/                   # Integration and API tests
└── docs/                   # Mintlify documentation
```

## How to Contribute

### Reporting Issues

- Use the [GitHub Issues](https://github.com/lagerdata/lager/issues) page
- Search existing issues before creating a new one
- Provide clear, detailed descriptions
- Include steps to reproduce for bugs
- Include relevant error messages or logs

### Pull Requests

1. **Fork the repository** and create your branch from `main`
2. **Make your changes** with clear, descriptive commits
3. **Test your changes** - ensure existing tests pass
4. **Update documentation** if needed
5. **Submit a pull request** with a clear description

### Code Style

- **Python:** Follow PEP 8 guidelines
- **Bash:** Use ShellCheck for linting
- **Commits:** Write clear, descriptive commit messages

### Testing

**Note:** Integration and API tests require physical Lager box hardware. Unit tests can be run without hardware.

```bash
# Unit tests (no hardware required)
cd test
pytest unit/

# Integration tests (requires a connected box)
./integration/power/supply.sh <box-name> <net-name>

# Python API tests (requires a connected box)
lager python test/api/power/test_supply_comprehensive.py --box <box-name>
```

## Development Guidelines

### Adding New CLI Commands

1. Create command module in `cli/commands/<category>/`
2. Create implementation script in `cli/impl/<category>/` if needed
3. Register command in `cli/main.py`
4. Add tests in `test/`

### Adding New Box Features

1. Add backend code in `box/lager/<category>/`
2. Add HTTP handlers in `box/lager/http_handlers/` if needed
3. Update box Docker container if dependencies change

## Code of Conduct

Please be respectful and constructive in all interactions. We are committed to providing a welcoming and inclusive environment for all contributors. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for details.

## License

By contributing to Lager, you agree that your contributions will be licensed under the Apache License 2.0.

## Questions?

If you have questions, feel free to:
- Open a GitHub issue
- Check existing documentation in `/docs`

Thank you for contributing!
