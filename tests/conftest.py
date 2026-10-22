import pytest

from bonis_netdev import samples


@pytest.fixture
def ros():
    return samples.ros_transport()


@pytest.fixture
def airos():
    return samples.airos_transport()
