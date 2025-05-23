# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from collections.abc import Iterable

import pytest

from airflow_breeze.global_constants import REGULAR_DOC_PACKAGES
from airflow_breeze.utils.packages import (
    PipRequirements,
    convert_cross_package_dependencies_to_table,
    convert_pip_requirements_to_table,
    expand_all_provider_distributions,
    find_matching_long_package_names,
    get_available_distributions,
    get_cross_provider_dependent_packages,
    get_dist_package_name_prefix,
    get_long_package_name,
    get_min_airflow_version,
    get_pip_package_name,
    get_previous_documentation_distribution_path,
    get_previous_source_providers_distribution_path,
    get_provider_info_dict,
    get_provider_requirements,
    get_removed_provider_ids,
    get_short_package_name,
    get_suspended_provider_folders,
    get_suspended_provider_ids,
    validate_provider_info_with_runtime_schema,
)
from airflow_breeze.utils.path_utils import AIRFLOW_ROOT_PATH, DOCS_ROOT


def test_get_available_packages():
    assert len(get_available_distributions()) > 70
    assert all(package not in REGULAR_DOC_PACKAGES for package in get_available_distributions())


def test_get_source_package_path():
    assert get_previous_source_providers_distribution_path("apache.hdfs") == AIRFLOW_ROOT_PATH.joinpath(
        "providers", "src", "airflow", "providers", "apache", "hdfs"
    )


def test_get_old_documentation_package_path():
    assert (
        get_previous_documentation_distribution_path("apache.hdfs")
        == DOCS_ROOT / "apache-airflow-providers-apache-hdfs"
    )


def test_expand_all_provider_distributions():
    assert len(expand_all_provider_distributions(("all-providers",))) > 70


def test_expand_all_provider_distributions_deduplicate_with_other_packages():
    assert len(expand_all_provider_distributions(("all-providers",))) == len(
        expand_all_provider_distributions(("all-providers", "amazon", "google"))
    )


def test_get_available_packages_include_non_provider_doc_packages():
    all_packages_including_regular_docs = get_available_distributions(include_non_provider_doc_packages=True)
    for package in REGULAR_DOC_PACKAGES:
        assert package in all_packages_including_regular_docs

    assert "all-providers" not in all_packages_including_regular_docs


def test_get_available_packages_include_non_provider_doc_packages_and_all_providers():
    all_packages_including_regular_docs = get_available_distributions(
        include_non_provider_doc_packages=True, include_all_providers=True
    )
    for package in REGULAR_DOC_PACKAGES:
        assert package in all_packages_including_regular_docs

    assert "all-providers" in all_packages_including_regular_docs


def test_get_short_package_name():
    assert get_short_package_name("apache-airflow") == "apache-airflow"
    assert get_short_package_name("docker-stack") == "docker-stack"
    assert get_short_package_name("apache-airflow-providers-amazon") == "amazon"
    assert get_short_package_name("apache-airflow-providers-apache-hdfs") == "apache.hdfs"


def test_error_on_get_short_package_name():
    with pytest.raises(ValueError, match="Invalid provider name"):
        get_short_package_name("wrong-provider-name")


def test_get_long_package_name():
    assert get_long_package_name("apache-airflow") == "apache-airflow"
    assert get_long_package_name("docker-stack") == "docker-stack"
    assert get_long_package_name("amazon") == "apache-airflow-providers-amazon"
    assert get_long_package_name("apache.hdfs") == "apache-airflow-providers-apache-hdfs"


def test_get_provider_requirements():
    # update me when asana dependencies change
    assert get_provider_requirements("asana") == ["apache-airflow>=2.9.0", "asana>=5.0.0"]


def test_get_removed_providers():
    # Modify it every time we schedule provider for removal or remove it
    assert get_removed_provider_ids() == []


def test_get_suspended_provider_ids():
    # Modify it every time we suspend/resume provider
    assert get_suspended_provider_ids() == []


def test_get_suspended_provider_folders():
    # Modify it every time we suspend/resume provider
    assert get_suspended_provider_folders() == []


@pytest.mark.parametrize(
    "short_packages, filters, long_packages",
    [
        (("amazon",), (), ("apache-airflow-providers-amazon",)),
        (("apache.hdfs",), (), ("apache-airflow-providers-apache-hdfs",)),
        (
            ("apache.hdfs",),
            ("apache-airflow-providers-amazon",),
            ("apache-airflow-providers-amazon", "apache-airflow-providers-apache-hdfs"),
        ),
        (
            ("apache.hdfs",),
            ("apache-airflow-providers-ama*",),
            ("apache-airflow-providers-amazon", "apache-airflow-providers-apache-hdfs"),
        ),
    ],
)
def test_find_matching_long_package_name(
    short_packages: tuple[str, ...], filters: tuple[str, ...], long_packages: tuple[str, ...]
):
    assert find_matching_long_package_names(short_packages=short_packages, filters=filters) == long_packages


def test_find_matching_long_package_name_bad_filter():
    with pytest.raises(SystemExit, match=r"Some filters did not find any package: \['bad-filter-\*"):
        find_matching_long_package_names(short_packages=(), filters=("bad-filter-*",))


@pytest.mark.parametrize(
    "provider_id, pip_package_name",
    [
        ("asana", "apache-airflow-providers-asana"),
        ("apache.hdfs", "apache-airflow-providers-apache-hdfs"),
    ],
)
def test_get_pip_package_name(provider_id: str, pip_package_name: str):
    assert get_pip_package_name(provider_id) == pip_package_name


@pytest.mark.parametrize(
    "provider_id, expected_package_name",
    [
        ("asana", "apache_airflow_providers_asana"),
        ("apache.hdfs", "apache_airflow_providers_apache_hdfs"),
    ],
)
def test_get_dist_package_name_prefix(provider_id: str, expected_package_name: str):
    assert get_dist_package_name_prefix(provider_id) == expected_package_name


@pytest.mark.parametrize(
    "requirement_string, expected",
    [
        pytest.param("apache-airflow", ("apache-airflow", ""), id="no-version-specifier"),
        pytest.param(
            "apache-airflow <2.7,>=2.5", ("apache-airflow", ">=2.5,<2.7"), id="range-version-specifier"
        ),
        pytest.param("watchtower~=3.0.1", ("watchtower", "~=3.0.1"), id="compat-version-specifier"),
        pytest.param("PyGithub!=1.58", ("PyGithub", "!=1.58"), id="not-equal-version-specifier"),
        pytest.param(
            "apache-airflow[amazon,google,microsoft.azure,docker]>2.7.0",
            ("apache-airflow[amazon,docker,google,microsoft.azure]", ">2.7.0"),
            id="package-with-extra",
        ),
        pytest.param(
            'mysql-connector-python>=8.0.11; platform_machine != "aarch64"',
            ("mysql-connector-python", '>=8.0.11; platform_machine != "aarch64"'),
            id="version-with-platform-marker",
        ),
        pytest.param(
            "pendulum>=2.1.2,<4.0;python_version<'3.12'",
            ("pendulum", '>=2.1.2,<4.0; python_version < "3.12"'),
            id="version-with-python-marker",
        ),
        pytest.param(
            "celery>=5.3.0,<6,!=5.3.3,!=5.3.2",
            ("celery", ">=5.3.0,!=5.3.2,!=5.3.3,<6"),
            id="complex-version-specifier",
        ),
        pytest.param(
            "apache-airflow; python_version<'3.12' or platform_machine != 'i386'",
            ("apache-airflow", '; python_version < "3.12" or platform_machine != "i386"'),
            id="no-version-specifier-with-complex-marker",
        ),
    ],
)
def test_parse_pip_requirements_parse(requirement_string: str, expected: tuple[str, str]):
    assert PipRequirements.from_requirement(requirement_string) == expected


@pytest.mark.parametrize(
    "requirements, markdown, table",
    [
        (
            ["apache-airflow>2.5.0", "apache-airflow-providers-http"],
            False,
            """
=================================  ==================
PIP package                        Version required
=================================  ==================
``apache-airflow``                 ``>2.5.0``
``apache-airflow-providers-http``
=================================  ==================
""",
        ),
        (
            ["apache-airflow>2.5.0", "apache-airflow-providers-http"],
            True,
            """
| PIP package                     | Version required   |
|:--------------------------------|:-------------------|
| `apache-airflow`                | `>2.5.0`           |
| `apache-airflow-providers-http` |                    |
""",
        ),
    ],
)
def test_convert_pip_requirements_to_table(requirements: Iterable[str], markdown: bool, table: str):
    assert convert_pip_requirements_to_table(requirements, markdown).strip() == table.strip()


def test_validate_provider_info_with_schema():
    for provider in get_available_distributions():
        validate_provider_info_with_runtime_schema(get_provider_info_dict(provider))


@pytest.mark.parametrize(
    "provider_id, min_version",
    [
        ("amazon", "2.9.0"),
        ("fab", "3.0.0.dev0"),
    ],
)
def test_get_min_airflow_version(provider_id: str, min_version: str):
    assert get_min_airflow_version(provider_id) == min_version


def test_convert_cross_package_dependencies_to_table():
    EXPECTED = """
| Dependent package                                                                   | Extra         |
|:------------------------------------------------------------------------------------|:--------------|
| [apache-airflow-providers-common-sql](https://airflow.apache.org/docs/common-sql)   | `common.sql`  |
| [apache-airflow-providers-google](https://airflow.apache.org/docs/google)           | `google`      |
| [apache-airflow-providers-openlineage](https://airflow.apache.org/docs/openlineage) | `openlineage` |
"""
    assert (
        convert_cross_package_dependencies_to_table(get_cross_provider_dependent_packages("trino")).strip()
        == EXPECTED.strip()
    )


def test_get_provider_info_dict():
    provider_info_dict = get_provider_info_dict("amazon")
    assert provider_info_dict["name"] == "Amazon"
    assert provider_info_dict["package-name"] == "apache-airflow-providers-amazon"
    assert "Amazon" in provider_info_dict["description"]
    assert provider_info_dict["filesystems"] == ["airflow.providers.amazon.aws.fs.s3"]
    assert len(provider_info_dict["integrations"]) > 35
    assert len(provider_info_dict["hooks"]) > 30
    assert len(provider_info_dict["triggers"]) > 15
    assert len(provider_info_dict["operators"]) > 20
    assert len(provider_info_dict["sensors"]) > 15
    assert len(provider_info_dict["transfers"]) > 15
    assert len(provider_info_dict["extra-links"]) > 5
    assert len(provider_info_dict["connection-types"]) > 3
    assert len(provider_info_dict["notifications"]) > 2
    assert len(provider_info_dict["secrets-backends"]) > 1
    assert len(provider_info_dict["logging"]) > 1
    assert len(provider_info_dict["config"].keys()) > 1
    assert len(provider_info_dict["executors"]) > 0
    assert len(provider_info_dict["dataset-uris"]) > 0
    assert len(provider_info_dict["dataset-uris"]) > 0
    assert len(provider_info_dict["asset-uris"]) > 0
