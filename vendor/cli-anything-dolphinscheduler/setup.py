"""setup.py for cli-anything-dolphinscheduler.

Package name: cli-anything-dolphinscheduler
Namespace: cli_anything.dolphinscheduler

This package uses PEP 420 namespace packaging:
- cli_anything/ has NO __init__.py (namespace package)
- cli_anything/dolphinscheduler/ HAS __init__.py (regular package)
- Multiple cli-anything-* packages can coexist in the same environment
"""

from pathlib import Path

from setuptools import find_namespace_packages, setup

# Read README if it exists
readme_path = Path(__file__).parent / "cli_anything" / "dolphinscheduler" / "README.md"
long_description = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

setup(
    name="cli-anything-dolphinscheduler",
    version="1.0.0",
    author="cli-anything",
    author_email="",
    description="Structured CLI harness for Apache DolphinScheduler",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/ChenFatMan/cli-anything-dolphinscheduler",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={
        "cli_anything.dolphinscheduler": ["skills/*.md", "README.md"],
    },
    python_requires=">=3.8",
    install_requires=[
        "click>=8.0.0",
        "requests>=2.25.0",
        "prompt-toolkit>=3.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=3.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "cli-anything-dolphinscheduler=cli_anything.dolphinscheduler.dolphinscheduler_cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
