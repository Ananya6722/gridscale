from pathlib import Path
from setuptools import setup

readme = Path(__file__).parent / "README.md"
long_description = readme.read_text(encoding="utf-8") if readme.exists() else ""

setup(
    name="gridscale",
    version="1.0.0",
    description="Zero-code-change, carbon- and cost-aware compute scheduler",
    long_description=long_description,
    long_description_content_type="text/markdown",
    license="MIT",
    py_modules=["gridscale"],
    python_requires=">=3.8",
    install_requires=["requests>=2.25.0", "rich>=10.0.0"],
    extras_require={"test": ["pytest>=7.0.0"]},
    entry_points={"console_scripts": ["gridscale=gridscale:main"]},
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)