"""Tests for package version checking functionality."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmcp.manifest.version_checker import (
    _version_cache,
    clear_version_cache,
    detect_package_type,
    get_npm_version,
    get_package_version,
    get_pypi_version,
    is_version_newer,
)


class TestDetectPackageType:
    """Tests for detect_package_type function."""

    def test_npx_simple_package(self) -> None:
        """Test detection of simple npx package."""
        pkg_type, pkg_name = detect_package_type("npx", ["-y", "playwright-mcp"])
        assert pkg_type == "npm"
        assert pkg_name == "playwright-mcp"

    def test_npx_scoped_package(self) -> None:
        """Test detection of scoped npm package."""
        pkg_type, pkg_name = detect_package_type("npx", ["-y", "@playwright/mcp"])
        assert pkg_type == "npm"
        assert pkg_name == "@playwright/mcp"

    def test_npx_package_with_latest(self) -> None:
        """Test detection strips @latest suffix."""
        pkg_type, pkg_name = detect_package_type("npx", ["-y", "some-package@latest"])
        assert pkg_type == "npm"
        assert pkg_name == "some-package"

    def test_npx_without_y_flag(self) -> None:
        """Test detection works without -y flag."""
        pkg_type, pkg_name = detect_package_type("npx", ["my-mcp-server"])
        assert pkg_type == "npm"
        assert pkg_name == "my-mcp-server"

    def test_npm_command(self) -> None:
        """Test detection with npm command picks first non-flag arg."""
        # Note: npm exec is treated as package name since code doesn't special-case it
        pkg_type, pkg_name = detect_package_type("npm", ["-y", "server-pkg"])
        assert pkg_type == "npm"
        assert pkg_name == "server-pkg"

    def test_uvx_simple_package(self) -> None:
        """Test detection of uvx (PyPI) package."""
        pkg_type, pkg_name = detect_package_type("uvx", ["mcp-server-git"])
        assert pkg_type == "pypi"
        assert pkg_name == "mcp-server-git"

    def test_uvx_with_flags(self) -> None:
        """Test uvx detection skips flags."""
        pkg_type, pkg_name = detect_package_type(
            "uvx", ["--quiet", "my-package", "--arg"]
        )
        assert pkg_type == "pypi"
        assert pkg_name == "my-package"

    def test_unknown_command(self) -> None:
        """Test unknown command returns unknown type."""
        pkg_type, pkg_name = detect_package_type("python", ["-m", "mymodule"])
        assert pkg_type == "unknown"
        assert pkg_name is None

    def test_docker_command(self) -> None:
        """Test docker command returns unknown."""
        pkg_type, pkg_name = detect_package_type("docker", ["run", "myimage"])
        assert pkg_type == "unknown"
        assert pkg_name is None

    def test_empty_args(self) -> None:
        """Test npx with empty args."""
        pkg_type, pkg_name = detect_package_type("npx", [])
        assert pkg_type == "unknown"
        assert pkg_name is None

    def test_only_flags(self) -> None:
        """Test npx with only flags."""
        pkg_type, pkg_name = detect_package_type("npx", ["-y", "--quiet"])
        assert pkg_type == "unknown"
        assert pkg_name is None


class TestIsVersionNewer:
    """Tests for is_version_newer function."""

    def test_same_version(self) -> None:
        """Test same versions are not newer."""
        assert is_version_newer("1.0.0", "1.0.0") is False
        assert is_version_newer("2025.1.1", "2025.1.1") is False

    def test_semver_patch_newer(self) -> None:
        """Test patch version comparison."""
        assert is_version_newer("1.0.0", "1.0.1") is True
        assert is_version_newer("1.0.1", "1.0.0") is False

    def test_semver_minor_newer(self) -> None:
        """Test minor version comparison."""
        assert is_version_newer("1.0.0", "1.1.0") is True
        assert is_version_newer("1.1.0", "1.0.0") is False

    def test_semver_major_newer(self) -> None:
        """Test major version comparison."""
        assert is_version_newer("1.0.0", "2.0.0") is True
        assert is_version_newer("2.0.0", "1.0.0") is False

    def test_date_based_version(self) -> None:
        """Test date-based version comparison."""
        assert is_version_newer("2025.1.1", "2025.1.2") is True
        assert is_version_newer("2025.1.1", "2025.2.1") is True
        assert is_version_newer("2025.12.1", "2025.1.1") is False

    def test_version_with_v_prefix(self) -> None:
        """Test versions with v prefix."""
        assert is_version_newer("v1.0.0", "v1.0.1") is True
        assert is_version_newer("V1.0.0", "V1.0.1") is True

    def test_short_version(self) -> None:
        """Test 2-part versions."""
        assert is_version_newer("1.0", "1.1") is True
        assert is_version_newer("0.19", "0.20") is True

    def test_different_length_versions(self) -> None:
        """Test versions with different number of parts."""
        # 1.0 vs 1.0.1 - tuple comparison: (1, 0) vs (1, 0, 1)
        assert is_version_newer("1.0", "1.0.1") is True
        assert is_version_newer("1.0.1", "1.0") is False

    def test_zero_versions(self) -> None:
        """Test pre-release style versions."""
        assert is_version_newer("0.0.1", "0.0.2") is True
        assert is_version_newer("0.0.19", "0.0.20") is True

    def test_non_numeric_versions(self) -> None:
        """Test non-numeric versions parse as empty tuples (equal)."""
        # Non-numeric strings have no numeric parts, so both parse as ()
        # () > () is False, so neither is "newer"
        assert is_version_newer("alpha", "beta") is False
        assert is_version_newer("beta", "alpha") is False

    def test_mixed_versions(self) -> None:
        """Test versions with mixed numeric and text parts."""
        # "rc1" parses as (1,), "rc2" parses as (2,)
        assert is_version_newer("1.0.0-rc1", "1.0.0-rc2") is True
        assert is_version_newer("v2.0-beta1", "v2.0-beta2") is True


class TestGetNpmVersion:
    """Tests for get_npm_version function."""

    @pytest.fixture(autouse=True)
    def clear_cache(self) -> None:
        """Clear version cache before each test."""
        clear_version_cache()

    @pytest.mark.asyncio
    async def test_successful_lookup(self) -> None:
        """Test successful npm version lookup."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"dist-tags": {"latest": "1.2.3"}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("test-package")
            assert version == "1.2.3"

    @pytest.mark.asyncio
    async def test_scoped_package_url_encoding(self) -> None:
        """Test scoped npm package URL encoding."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"dist-tags": {"latest": "0.1.0"}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("@playwright/mcp")
            assert version == "0.1.0"
            # Check URL was properly encoded
            call_args = mock_session.get.call_args
            url = call_args[0][0]
            assert "%40" in url  # @ encoded
            assert "%2F" in url  # / encoded

    @pytest.mark.asyncio
    async def test_404_response(self) -> None:
        """Test handling of 404 response."""
        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("nonexistent-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        """Test handling of timeout."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("test-package", timeout=0.1)
            assert version is None

    @pytest.mark.asyncio
    async def test_network_error(self) -> None:
        """Test handling of network error."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=Exception("Network error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("test-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_missing_dist_tags(self) -> None:
        """Test handling of response missing dist-tags."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"name": "test"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("test-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """Test that cached versions are returned without network call."""
        # Populate cache
        _version_cache["npm:cached-package"] = "1.0.0"

        # Should not make network call
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=Exception("Should not be called"))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_npm_version("cached-package")
            assert version == "1.0.0"
            mock_session.get.assert_not_called()


class TestGetPypiVersion:
    """Tests for get_pypi_version function."""

    @pytest.fixture(autouse=True)
    def clear_cache(self) -> None:
        """Clear version cache before each test."""
        clear_version_cache()

    @pytest.mark.asyncio
    async def test_successful_lookup(self) -> None:
        """Test successful PyPI version lookup."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"info": {"version": "2025.12.18"}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_pypi_version("mcp-server-git")
            assert version == "2025.12.18"

    @pytest.mark.asyncio
    async def test_correct_url(self) -> None:
        """Test PyPI URL is correct."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"info": {"version": "1.0.0"}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await get_pypi_version("my-package")
            call_args = mock_session.get.call_args
            url = call_args[0][0]
            assert url == "https://pypi.org/pypi/my-package/json"

    @pytest.mark.asyncio
    async def test_404_response(self) -> None:
        """Test handling of 404 response."""
        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_pypi_version("nonexistent-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        """Test handling of timeout."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_pypi_version("test-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_missing_info(self) -> None:
        """Test handling of response missing info."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"name": "test"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            version = await get_pypi_version("test-package")
            assert version is None

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """Test that cached versions are returned without network call."""
        _version_cache["pypi:cached-package"] = "2.0.0"

        version = await get_pypi_version("cached-package")
        assert version == "2.0.0"


class TestGetPackageVersion:
    """Tests for get_package_version function."""

    @pytest.fixture(autouse=True)
    def clear_cache(self) -> None:
        """Clear version cache before each test."""
        clear_version_cache()

    @pytest.mark.asyncio
    async def test_npm_package(self) -> None:
        """Test npm package version lookup."""
        with patch(
            "pmcp.manifest.version_checker.get_npm_version",
            new_callable=AsyncMock,
            return_value="1.0.0",
        ):
            version, pkg_type = await get_package_version("npx", ["-y", "my-package"])
            assert version == "1.0.0"
            assert pkg_type == "npm"

    @pytest.mark.asyncio
    async def test_pypi_package(self) -> None:
        """Test PyPI package version lookup."""
        with patch(
            "pmcp.manifest.version_checker.get_pypi_version",
            new_callable=AsyncMock,
            return_value="2.0.0",
        ):
            version, pkg_type = await get_package_version("uvx", ["my-package"])
            assert version == "2.0.0"
            assert pkg_type == "pypi"

    @pytest.mark.asyncio
    async def test_unknown_package(self) -> None:
        """Test unknown package type."""
        version, pkg_type = await get_package_version("docker", ["run", "image"])
        assert version is None
        assert pkg_type == "unknown"

    @pytest.mark.asyncio
    async def test_npm_lookup_failure(self) -> None:
        """Test handling of npm lookup failure."""
        with patch(
            "pmcp.manifest.version_checker.get_npm_version",
            new_callable=AsyncMock,
            return_value=None,
        ):
            version, pkg_type = await get_package_version("npx", ["-y", "my-package"])
            assert version is None
            assert pkg_type == "npm"


class TestClearVersionCache:
    """Tests for clear_version_cache function."""

    def test_clears_cache(self) -> None:
        """Test cache is cleared."""
        _version_cache["npm:test"] = "1.0.0"
        _version_cache["pypi:test"] = "2.0.0"
        assert len(_version_cache) == 2

        clear_version_cache()
        assert len(_version_cache) == 0
