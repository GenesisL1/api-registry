// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Read surface of the registry, as seen by the lens.
interface IGenesisL1APIRegistry {
    struct Instance {
        uint64 id;
        address owner;
        uint64 addedAt;
        uint64 updatedAt;
        uint16 endpointCount;
        bool active;
        string name;
        string region;
        string operatorInfo;
    }

    struct Endpoint {
        uint64 id;
        uint64 instanceId;
        uint64 addedAt;
        uint64 updatedAt;
        uint32 epoch;
        uint32 adverseDayCount;
        uint32 lastAdverseDay;
        uint8 kind;
        bool active;
        bool unreliable;
        bool verified;
        bytes32 fingerprint;
        string url;
    }

    struct Checker {
        bool authorized;
        uint64 addedAt;
        uint64 updatedAt;
        uint32 reportCount;
        string name;
        string telemetryUrl;
    }

    struct DayAggregate {
        uint32 day;
        uint32 epoch;
        uint16 reportCount;
        uint16 adverseCount;
        uint16 healthyCount;
        uint16 uptimeBpsAverage;
        uint32 latencyP95AverageMs;
        uint32 errorBpsAverage;
        uint64 lastReportedAt;
    }

    function revision() external view returns (uint64);
    function admin() external view returns (address);
    function feeRecipient() external view returns (address payable);
    function endpointWriteFee() external view returns (uint256);
    function reliabilityThresholdBps() external view returns (uint16);
    function adverseReportQuorum() external view returns (uint8);
    function verificationQuorum() external view returns (uint8);
    function authorizedCheckerCount() external view returns (uint16);
    function activeInstanceCount() external view returns (uint256);

    function activeInstanceIds(uint256 cursor, uint256 limit)
        external
        view
        returns (uint64[] memory ids, uint256 nextCursor, uint64 atRevision);

    function getInstance(uint64 instanceId) external view returns (Instance memory);
    function getEndpoint(uint64 endpointId) external view returns (Endpoint memory);
    function getInstanceEndpointIds(uint64 instanceId) external view returns (uint64[] memory);
    function getLatestAggregate(uint64 endpointId) external view returns (DayAggregate memory);
    function getCheckerAddresses() external view returns (address[] memory);
    function getChecker(address checker) external view returns (Checker memory);
    function verificationCount(uint64 endpointId, uint32 epoch) external view returns (uint16);
}

/// @title  GenesisL1 Registry Lens
/// @notice Stateless read aggregator. Holds no funds and no state, so it can be
///         redeployed whenever clients need a different shape of data without
///         ever touching the registry that holds the records.
/// @dev    Every function here is view-only and intended for `eth_call`. Page
///         sizes are the caller's responsibility; 25 instances per page keeps a
///         response comfortably inside default node response limits.
contract GenesisL1RegistryLens {
    IGenesisL1APIRegistry public immutable registry;

    struct DirectoryPage {
        IGenesisL1APIRegistry.Instance[] instances;
        uint16[] endpointCounts;
        IGenesisL1APIRegistry.Endpoint[] endpoints;
        IGenesisL1APIRegistry.DayAggregate[] aggregates;
        uint16[] verifications;
        uint256 nextCursor;
        uint256 totalInstances;
        uint64 atRevision;
    }

    struct Summary {
        uint64 revision;
        uint256 activeInstances;
        uint256 activeEndpoints;
        uint256 endpointWriteFee;
        uint16 reliabilityThresholdBps;
        uint8 adverseReportQuorum;
        uint8 verificationQuorum;
        uint16 authorizedCheckerCount;
        address admin;
        address feeRecipient;
    }

    struct CheckerView {
        address addr;
        bool authorized;
        uint32 reportCount;
        string name;
        string telemetryUrl;
    }

    error InvalidInput();

    constructor(address registry_) {
        if (registry_ == address(0)) revert InvalidInput();
        registry = IGenesisL1APIRegistry(registry_);
    }

    /// @notice One call returns everything needed to render a page of the
    ///         directory: live instances, their endpoints flattened with the
    ///         counts needed to regroup them, the quorum-verified on-chain
    ///         aggregate for each endpoint, and how many checkers have attested
    ///         to each endpoint's proof of control.
    /// @dev    `atRevision` is read last. If it differs from the registry's
    ///         current revision the caller read across a write and should retry.
    function getDirectory(uint256 cursor, uint256 limit) external view returns (DirectoryPage memory page) {
        (uint64[] memory instanceIds, uint256 nextCursor, uint64 atRevision) = registry.activeInstanceIds(cursor, limit);
        page.nextCursor = nextCursor;
        page.atRevision = atRevision;
        page.totalInstances = registry.activeInstanceCount();

        uint256 size = instanceIds.length;
        page.instances = new IGenesisL1APIRegistry.Instance[](size);
        page.endpointCounts = new uint16[](size);

        uint64[] memory endpointIds = _collectEndpointIds(instanceIds, page.endpointCounts);
        for (uint256 i; i < size; ++i) page.instances[i] = registry.getInstance(instanceIds[i]);

        uint256 total = endpointIds.length;
        page.endpoints = new IGenesisL1APIRegistry.Endpoint[](total);
        page.aggregates = new IGenesisL1APIRegistry.DayAggregate[](total);
        page.verifications = new uint16[](total);
        for (uint256 i; i < total; ++i) {
            IGenesisL1APIRegistry.Endpoint memory endpoint = registry.getEndpoint(endpointIds[i]);
            page.endpoints[i] = endpoint;
            page.aggregates[i] = registry.getLatestAggregate(endpointIds[i]);
            page.verifications[i] = registry.verificationCount(endpointIds[i], endpoint.epoch);
        }
    }

    /// @notice Every active endpoint, for checkers that need the full working set.
    function getAllEndpoints(uint256 cursor, uint256 limit)
        external
        view
        returns (IGenesisL1APIRegistry.Endpoint[] memory endpoints, address[] memory owners, uint256 nextCursor, uint64 atRevision)
    {
        (uint64[] memory instanceIds, uint256 next, uint64 rev) = registry.activeInstanceIds(cursor, limit);
        uint16[] memory counts = new uint16[](instanceIds.length);
        uint64[] memory endpointIds = _collectEndpointIds(instanceIds, counts);

        endpoints = new IGenesisL1APIRegistry.Endpoint[](endpointIds.length);
        owners = new address[](endpointIds.length);
        uint256 written;
        for (uint256 i; i < instanceIds.length; ++i) {
            address owner = registry.getInstance(instanceIds[i]).owner;
            for (uint256 j; j < counts[i]; ++j) {
                endpoints[written] = registry.getEndpoint(endpointIds[written]);
                owners[written] = owner;
                written++;
            }
        }
        nextCursor = next;
        atRevision = rev;
    }

    function getSummary() external view returns (Summary memory summary) {
        summary.revision = registry.revision();
        summary.activeInstances = registry.activeInstanceCount();
        summary.endpointWriteFee = registry.endpointWriteFee();
        summary.reliabilityThresholdBps = registry.reliabilityThresholdBps();
        summary.adverseReportQuorum = registry.adverseReportQuorum();
        summary.verificationQuorum = registry.verificationQuorum();
        summary.authorizedCheckerCount = registry.authorizedCheckerCount();
        summary.admin = registry.admin();
        summary.feeRecipient = registry.feeRecipient();
        summary.activeEndpoints = _countActiveEndpoints();
    }

    function getCheckers() external view returns (CheckerView[] memory values) {
        address[] memory addresses = registry.getCheckerAddresses();
        values = new CheckerView[](addresses.length);
        for (uint256 i; i < addresses.length; ++i) {
            IGenesisL1APIRegistry.Checker memory checker = registry.getChecker(addresses[i]);
            values[i] = CheckerView({
                addr: addresses[i],
                authorized: checker.authorized,
                reportCount: checker.reportCount,
                name: checker.name,
                telemetryUrl: checker.telemetryUrl
            });
        }
    }

    function getEndpointsByIds(uint64[] calldata ids)
        external
        view
        returns (IGenesisL1APIRegistry.Endpoint[] memory endpoints, IGenesisL1APIRegistry.DayAggregate[] memory aggregates)
    {
        endpoints = new IGenesisL1APIRegistry.Endpoint[](ids.length);
        aggregates = new IGenesisL1APIRegistry.DayAggregate[](ids.length);
        for (uint256 i; i < ids.length; ++i) {
            endpoints[i] = registry.getEndpoint(ids[i]);
            aggregates[i] = registry.getLatestAggregate(ids[i]);
        }
    }

    // ------------------------------------------------------------- internals

    function _collectEndpointIds(uint64[] memory instanceIds, uint16[] memory counts)
        internal
        view
        returns (uint64[] memory flat)
    {
        uint256 size = instanceIds.length;
        uint64[][] memory perInstance = new uint64[][](size);
        uint256 total;
        for (uint256 i; i < size; ++i) {
            uint64[] memory ids = registry.getInstanceEndpointIds(instanceIds[i]);
            uint256 active;
            for (uint256 j; j < ids.length; ++j) {
                if (registry.getEndpoint(ids[j]).active) {
                    ids[active] = ids[j];
                    active++;
                }
            }
            assembly {
                mstore(ids, active)
            }
            perInstance[i] = ids;
            counts[i] = uint16(active);
            total += active;
        }
        flat = new uint64[](total);
        uint256 written;
        for (uint256 i; i < size; ++i) {
            uint64[] memory ids = perInstance[i];
            for (uint256 j; j < ids.length; ++j) {
                flat[written] = ids[j];
                written++;
            }
        }
    }

    function _countActiveEndpoints() internal view returns (uint256 total) {
        (uint64[] memory instanceIds,,) = registry.activeInstanceIds(0, type(uint32).max);
        for (uint256 i; i < instanceIds.length; ++i) {
            uint64[] memory ids = registry.getInstanceEndpointIds(instanceIds[i]);
            for (uint256 j; j < ids.length; ++j) {
                if (registry.getEndpoint(ids[j]).active) total++;
            }
        }
    }
}
