// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title  GenesisL1 Public API Registry
/// @notice Permissionless registry of public GenesisL1 API infrastructure.
///         Endpoint records live on-chain. Fine-grained telemetry does not:
///         authorized checkers anchor only compact daily attestations, and only
///         when an endpoint is failing its reliability target or recovering from
///         a previous failure. Healthy endpoints in good standing never write.
/// @dev    Deploy with solc 0.8.24 exactly. Legacy codegen, optimizer on,
///         200 runs. Set evmVersion to the chain's supported target (paris for
///         GenesisL1 v1.6.2). Do not rely on viaIR.
contract GenesisL1APIRegistry {
    // ---------------------------------------------------------------- limits

    uint16 public constant MAX_INSTANCES_PER_OWNER = 4;
    uint16 public constant MAX_ENDPOINTS_PER_INSTANCE = 24;
    uint256 public constant MAX_URL_BYTES = 256;
    uint256 public constant MAX_NAME_BYTES = 96;
    uint256 public constant MAX_INFO_BYTES = 256;
    uint256 public constant MAX_REASON_BYTES = 320;
    uint16 public constant BPS = 10_000;

    bytes32 private constant _HTTPS = keccak256("https://");
    bytes32 private constant _HTTP = keccak256("http://");
    bytes32 private constant _WSS = keccak256("wss://");
    bytes32 private constant _WS = keccak256("ws://");
    bytes32 private constant _GRPCS = keccak256("grpcs://");
    bytes32 private constant _GRPC = keccak256("grpc://");

    /// @notice Quorum can never be lowered to a single reporter by any admin.
    uint8 public constant MIN_QUORUM = 2;
    /// @notice Reliability target can never be set to an unreachable value.
    uint16 public constant MIN_THRESHOLD_BPS = 5_000;
    /// @notice Hard ceiling on the write fee, so the registry cannot be priced shut.
    uint256 public constant MAX_WRITE_FEE = 1_000 ether;
    /// @notice Delay applied to every privilege- or economics-changing action.
    uint256 public constant TIMELOCK_DELAY = 2 days;

    // ----------------------------------------------------------------- types

    enum EndpointKind {
        EVM_HTTP,
        EVM_WEBSOCKET,
        COSMOS_LCD,
        COMET_RPC,
        COSMOS_GRPC,
        COSMOS_GRPC_WEB,
        INDEXER,
        SNAPSHOT,
        OTHER
    }

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
        uint32 adverseDayCount; // lifetime; never reset by an epoch change
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

    /// @notice One checker's daily statement about one endpoint epoch. A single
    ///         entry can carry a measurement, a proof-of-control attestation, or
    ///         both, so a checker makes at most one on-chain call per day.
    struct Attestation {
        uint64 endpointId;
        uint32 epoch;
        uint16 uptimeBps;
        uint32 latencyP95Ms;
        uint16 errorBps;
        bool hasMetrics;
        bool controlVerified;
    }

    /// @notice Quorum-aggregated view of one endpoint's day.
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

    struct CheckerReport {
        uint32 day;
        uint32 epoch;
        uint16 uptimeBps;
        uint32 latencyP95Ms;
        uint16 errorBps;
    }

    struct PendingConfig {
        uint256 writeFee;
        uint16 thresholdBps;
        uint8 adverseQuorum;
        uint8 verificationQuorum;
        uint64 eta;
    }

    struct PendingCheckerAuth {
        uint64 eta;
        string name;
        string telemetryUrl;
    }

    // ----------------------------------------------------------------- state

    address public admin;
    address public pendingAdmin;
    address payable public feeRecipient;
    address payable public pendingFeeRecipient;
    uint64 public feeRecipientEta;

    uint256 public endpointWriteFee;
    uint16 public reliabilityThresholdBps;
    uint8 public adverseReportQuorum;
    uint8 public verificationQuorum;

    uint64 public nextInstanceId = 1;
    uint64 public nextEndpointId = 1;
    /// @notice Bumped on every state-changing registry write. Clients poll this
    ///         single word to decide whether a full re-read is needed.
    uint64 public revision;

    PendingConfig public pendingConfig;

    mapping(uint64 => Instance) private _instances;
    mapping(uint64 => Endpoint) private _endpoints;
    mapping(uint64 => uint64[]) private _instanceEndpointIds;
    mapping(address => uint64[]) private _ownerInstanceIds;
    mapping(address => uint16) public ownerActiveInstanceCount;

    uint64[] private _activeInstanceIds;
    mapping(uint64 => uint256) private _activeInstanceIndex; // id => index+1

    mapping(uint64 => address) public instanceOfferedTo;

    mapping(address => Checker) private _checkers;
    mapping(address => PendingCheckerAuth) private _pendingCheckerAuth;
    address[] private _checkerAddresses;
    uint16 public authorizedCheckerCount;

    mapping(uint64 => DayAggregate) private _latestAggregate;
    mapping(uint64 => mapping(address => CheckerReport)) private _checkerLatestReport;

    // endpointId => epoch => checker => attested
    mapping(uint64 => mapping(uint32 => mapping(address => bool))) private _verifiedBy;
    mapping(uint64 => mapping(uint32 => uint16)) private _verificationCount;

    uint256 private _guard = 1;

    // ---------------------------------------------------------------- events

    event AdminTransferStarted(address indexed currentAdmin, address indexed pendingAdmin);
    event AdminTransferred(address indexed previousAdmin, address indexed newAdmin);
    event FeeRecipientProposed(address indexed newRecipient, uint64 eta);
    event FeeRecipientUpdated(address indexed previousRecipient, address indexed newRecipient);
    event ConfigurationProposed(uint256 writeFee, uint16 thresholdBps, uint8 adverseQuorum, uint8 verificationQuorum, uint64 eta);
    event ConfigurationCancelled();
    event ConfigurationUpdated(uint256 writeFee, uint16 thresholdBps, uint8 adverseQuorum, uint8 verificationQuorum);

    event InstanceCreated(uint64 indexed instanceId, address indexed owner, string name, uint64 timestamp);
    event InstanceUpdated(uint64 indexed instanceId, string name, uint64 timestamp);
    event InstanceDeleted(uint64 indexed instanceId, address indexed owner, uint64 timestamp);
    event InstanceOffered(uint64 indexed instanceId, address indexed from, address indexed to);
    event InstanceTransferred(uint64 indexed instanceId, address indexed from, address indexed to);

    event EndpointAdded(uint64 indexed endpointId, uint64 indexed instanceId, uint8 kind, string url, uint32 epoch, uint64 timestamp);
    event EndpointUpdated(uint64 indexed endpointId, uint64 indexed instanceId, uint8 kind, string url, uint32 epoch, uint64 timestamp);
    event EndpointDeleted(uint64 indexed endpointId, uint64 indexed instanceId, uint64 timestamp);
    event EndpointReliabilityChanged(uint64 indexed endpointId, bool unreliable, uint32 epoch, uint64 timestamp);
    event EndpointVerified(uint64 indexed endpointId, uint32 epoch, uint16 attestations, uint64 timestamp);
    event EndpointContested(uint64 indexed endpointId, address indexed owner, string reason, uint64 timestamp);

    event CheckerAuthorizationProposed(address indexed checker, string name, uint64 eta);
    event CheckerAuthorizationCancelled(address indexed checker);
    event CheckerAuthorized(address indexed checker, string name, string telemetryUrl, uint64 timestamp);
    event CheckerRevoked(address indexed checker, uint64 timestamp);
    event CheckerTelemetryUpdated(address indexed checker, string telemetryUrl);
    event ReportSubmitted(uint64 indexed endpointId, address indexed checker, uint32 indexed day, uint32 epoch, uint16 uptimeBps, uint32 latencyP95Ms, uint16 errorBps);
    event FeesWithdrawn(address indexed recipient, uint256 amount);

    // ---------------------------------------------------------------- errors

    error Unauthorized();
    error InvalidInput();
    error NotFound();
    error LimitReached();
    error IncorrectFee();
    error NothingPending();
    error TimelockPending();
    error TransferFailed();

    // -------------------------------------------------------------- modifiers

    modifier onlyAdmin() {
        if (msg.sender != admin) revert Unauthorized();
        _;
    }

    modifier onlyChecker() {
        if (!_checkers[msg.sender].authorized) revert Unauthorized();
        _;
    }

    modifier nonReentrant() {
        if (_guard != 1) revert Unauthorized();
        _guard = 2;
        _;
        _guard = 1;
    }

    /// @param admin_ registry administrator (checker set, parameters)
    /// @param feeRecipient_ destination for accrued write fees; use a multisig
    /// @param endpointWriteFee_ fee per endpoint write, in wei (1e18 == 1 L1 coin)
    constructor(address admin_, address payable feeRecipient_, uint256 endpointWriteFee_) {
        if (admin_ == address(0) || feeRecipient_ == address(0)) revert InvalidInput();
        if (endpointWriteFee_ > MAX_WRITE_FEE) revert InvalidInput();
        admin = admin_;
        feeRecipient = feeRecipient_;
        endpointWriteFee = endpointWriteFee_;
        reliabilityThresholdBps = 9_900;
        adverseReportQuorum = MIN_QUORUM;
        verificationQuorum = MIN_QUORUM;
        emit AdminTransferred(address(0), admin_);
        emit FeeRecipientUpdated(address(0), feeRecipient_);
        emit ConfigurationUpdated(endpointWriteFee_, 9_900, MIN_QUORUM, MIN_QUORUM);
    }

    // ------------------------------------------------------- operator writes

    function createInstance(
        string calldata name,
        string calldata region,
        string calldata operatorInfo,
        EndpointKind[] calldata kinds,
        string[] calldata urls
    ) external payable nonReentrant returns (uint64 instanceId) {
        if (ownerActiveInstanceCount[msg.sender] >= MAX_INSTANCES_PER_OWNER) revert LimitReached();
        _validateMetadata(name, region, operatorInfo);
        uint256 count = kinds.length;
        if (count == 0 || count != urls.length || count > MAX_ENDPOINTS_PER_INSTANCE) revert InvalidInput();
        _collectFee(count);

        instanceId = _writeInstance(name, region, operatorInfo);
        uint64 now64 = uint64(block.timestamp);
        for (uint256 i; i < count; ++i) _addEndpoint(instanceId, kinds[i], urls[i], now64);
        revision++;
    }

    function _writeInstance(string calldata name, string calldata region, string calldata operatorInfo)
        internal
        returns (uint64 instanceId)
    {
        instanceId = nextInstanceId++;
        uint64 now64 = uint64(block.timestamp);
        Instance storage instance = _instances[instanceId];
        instance.id = instanceId;
        instance.owner = msg.sender;
        instance.addedAt = now64;
        instance.updatedAt = now64;
        instance.active = true;
        instance.name = name;
        instance.region = region;
        instance.operatorInfo = operatorInfo;

        _ownerInstanceIds[msg.sender].push(instanceId);
        ownerActiveInstanceCount[msg.sender]++;
        _activeInstanceIds.push(instanceId);
        _activeInstanceIndex[instanceId] = _activeInstanceIds.length;
        emit InstanceCreated(instanceId, msg.sender, name, now64);
    }

    function updateInstance(
        uint64 instanceId,
        string calldata name,
        string calldata region,
        string calldata operatorInfo
    ) external payable nonReentrant {
        Instance storage instance = _ownedActiveInstance(instanceId);
        _validateMetadata(name, region, operatorInfo);
        _collectFee(1);
        instance.name = name;
        instance.region = region;
        instance.operatorInfo = operatorInfo;
        instance.updatedAt = uint64(block.timestamp);
        revision++;
        emit InstanceUpdated(instanceId, name, instance.updatedAt);
    }

    function deleteInstance(uint64 instanceId) external {
        Instance storage instance = _ownedActiveInstance(instanceId);
        uint64 now64 = uint64(block.timestamp);
        instance.active = false;
        instance.updatedAt = now64;
        instance.endpointCount = 0;
        ownerActiveInstanceCount[msg.sender]--;
        _removeActiveInstance(instanceId);

        uint64[] storage ids = _instanceEndpointIds[instanceId];
        uint256 length = ids.length;
        for (uint256 i; i < length; ++i) {
            Endpoint storage endpoint = _endpoints[ids[i]];
            if (endpoint.active) {
                endpoint.active = false;
                endpoint.updatedAt = now64;
                emit EndpointDeleted(endpoint.id, instanceId, now64);
            }
        }
        revision++;
        emit InstanceDeleted(instanceId, msg.sender, now64);
    }

    /// @notice Two-step instance handover. A mistyped address cannot strand an instance.
    function offerInstance(uint64 instanceId, address to) external {
        _ownedActiveInstance(instanceId);
        instanceOfferedTo[instanceId] = to;
        emit InstanceOffered(instanceId, msg.sender, to);
    }

    function claimInstance(uint64 instanceId) external {
        if (instanceOfferedTo[instanceId] != msg.sender) revert Unauthorized();
        if (ownerActiveInstanceCount[msg.sender] >= MAX_INSTANCES_PER_OWNER) revert LimitReached();
        Instance storage instance = _instances[instanceId];
        if (!instance.active) revert NotFound();
        address previous = instance.owner;
        ownerActiveInstanceCount[previous]--;
        ownerActiveInstanceCount[msg.sender]++;
        instance.owner = msg.sender;
        instance.updatedAt = uint64(block.timestamp);
        _ownerInstanceIds[msg.sender].push(instanceId);
        delete instanceOfferedTo[instanceId];
        revision++;
        emit InstanceTransferred(instanceId, previous, msg.sender);
    }

    function addEndpoints(uint64 instanceId, EndpointKind[] calldata kinds, string[] calldata urls)
        external
        payable
        nonReentrant
        returns (uint64[] memory ids)
    {
        Instance storage instance = _ownedActiveInstance(instanceId);
        uint256 count = kinds.length;
        if (count == 0 || count != urls.length) revert InvalidInput();
        if (uint256(instance.endpointCount) + count > MAX_ENDPOINTS_PER_INSTANCE) revert LimitReached();
        _collectFee(count);
        ids = new uint64[](count);
        uint64 now64 = uint64(block.timestamp);
        for (uint256 i; i < count; ++i) ids[i] = _addEndpoint(instanceId, kinds[i], urls[i], now64);
        revision++;
    }

    /// @notice Editing an endpoint always costs the fee. The measurement epoch
    ///         advances only when the URL or kind actually changed, so a paid
    ///         no-op cannot be used to erase a reliability record.
    function updateEndpoint(uint64 endpointId, EndpointKind kind, string calldata url)
        external
        payable
        nonReentrant
    {
        Endpoint storage endpoint = _ownedActiveEndpoint(endpointId);
        _collectFee(1);
        _validateUrl(url);

        bytes32 fingerprint = keccak256(abi.encodePacked(uint8(kind), url));
        uint64 now64 = uint64(block.timestamp);
        endpoint.kind = uint8(kind);
        endpoint.url = url;
        endpoint.updatedAt = now64;

        if (fingerprint != endpoint.fingerprint) {
            endpoint.fingerprint = fingerprint;
            endpoint.epoch++;
            endpoint.verified = false;
            if (endpoint.unreliable) {
                endpoint.unreliable = false;
                emit EndpointReliabilityChanged(endpointId, false, endpoint.epoch, now64);
            }
            delete _latestAggregate[endpointId];
        }
        revision++;
        emit EndpointUpdated(endpointId, endpoint.instanceId, uint8(kind), url, endpoint.epoch, now64);
    }

    function deleteEndpoint(uint64 endpointId) external {
        Endpoint storage endpoint = _ownedActiveEndpoint(endpointId);
        endpoint.active = false;
        endpoint.updatedAt = uint64(block.timestamp);
        _instances[endpoint.instanceId].endpointCount--;
        revision++;
        emit EndpointDeleted(endpointId, endpoint.instanceId, endpoint.updatedAt);
    }

    function reactivateEndpoint(uint64 endpointId) external payable nonReentrant {
        Endpoint storage endpoint = _endpoints[endpointId];
        Instance storage instance = _instances[endpoint.instanceId];
        if (endpoint.id == 0 || instance.owner != msg.sender || !instance.active || endpoint.active) revert Unauthorized();
        if (instance.endpointCount >= MAX_ENDPOINTS_PER_INSTANCE) revert LimitReached();
        _collectFee(1);
        uint64 now64 = uint64(block.timestamp);
        endpoint.active = true;
        endpoint.unreliable = false;
        endpoint.verified = false;
        endpoint.updatedAt = now64;
        endpoint.epoch++;
        instance.endpointCount++;
        delete _latestAggregate[endpointId];
        revision++;
        emit EndpointUpdated(endpointId, endpoint.instanceId, endpoint.kind, endpoint.url, endpoint.epoch, now64);
    }

    /// @notice Operator's public right of reply to a reliability flag. Free, and
    ///         recorded as an event so disputes are auditable.
    function contestEndpoint(uint64 endpointId, string calldata reason) external {
        Endpoint storage endpoint = _endpoints[endpointId];
        if (endpoint.id == 0) revert NotFound();
        if (_instances[endpoint.instanceId].owner != msg.sender) revert Unauthorized();
        if (bytes(reason).length == 0 || bytes(reason).length > MAX_REASON_BYTES) revert InvalidInput();
        emit EndpointContested(endpointId, msg.sender, reason, uint64(block.timestamp));
    }

    // -------------------------------------------------------- checker writes

    /// @notice Submit a day's attestations. A measurement is recorded only when
    ///         it is adverse, or when it is evidence of recovery for an endpoint
    ///         that is currently flagged; anything else is skipped rather than
    ///         reverted, so one stale entry can never discard a whole batch.
    ///         Healthy endpoints in good standing never cause a storage write.
    function attest(Attestation[] calldata items) external onlyChecker {
        uint256 length = items.length;
        if (length == 0) revert InvalidInput();
        uint32 day = uint32(block.timestamp / 1 days);
        uint32 applied;
        for (uint256 i; i < length; ++i) {
            if (items[i].controlVerified) _applyVerification(items[i].endpointId, items[i].epoch);
            if (items[i].hasMetrics && _applyReport(items[i], day)) applied++;
        }
        if (applied != 0) _checkers[msg.sender].reportCount += applied;
    }

    function setCheckerTelemetry(string calldata telemetryUrl) external onlyChecker {
        if (bytes(telemetryUrl).length > MAX_URL_BYTES) revert InvalidInput();
        _checkers[msg.sender].telemetryUrl = telemetryUrl;
        _checkers[msg.sender].updatedAt = uint64(block.timestamp);
        emit CheckerTelemetryUpdated(msg.sender, telemetryUrl);
    }

    // ---------------------------------------------------------- admin writes

    function proposeChecker(address checker, string calldata name, string calldata telemetryUrl) external onlyAdmin {
        if (checker == address(0) || bytes(name).length == 0 || bytes(name).length > MAX_NAME_BYTES) revert InvalidInput();
        if (bytes(telemetryUrl).length > MAX_URL_BYTES) revert InvalidInput();
        uint64 eta = uint64(block.timestamp + TIMELOCK_DELAY);
        _pendingCheckerAuth[checker] = PendingCheckerAuth({eta: eta, name: name, telemetryUrl: telemetryUrl});
        emit CheckerAuthorizationProposed(checker, name, eta);
    }

    function cancelChecker(address checker) external onlyAdmin {
        if (_pendingCheckerAuth[checker].eta == 0) revert NothingPending();
        delete _pendingCheckerAuth[checker];
        emit CheckerAuthorizationCancelled(checker);
    }

    function commitChecker(address checker) external onlyAdmin {
        PendingCheckerAuth storage pending = _pendingCheckerAuth[checker];
        if (pending.eta == 0) revert NothingPending();
        if (block.timestamp < pending.eta) revert TimelockPending();
        Checker storage item = _checkers[checker];
        uint64 now64 = uint64(block.timestamp);
        if (item.addedAt == 0) {
            item.addedAt = now64;
            _checkerAddresses.push(checker);
        }
        if (!item.authorized) authorizedCheckerCount++;
        item.authorized = true;
        item.name = pending.name;
        item.telemetryUrl = pending.telemetryUrl;
        item.updatedAt = now64;
        emit CheckerAuthorized(checker, item.name, item.telemetryUrl, now64);
        delete _pendingCheckerAuth[checker];
    }

    /// @notice Revocation is immediate. Authorization is delayed; removing a
    ///         misbehaving checker must never wait on a timelock.
    function revokeChecker(address checker) external onlyAdmin {
        Checker storage item = _checkers[checker];
        if (!item.authorized) revert NotFound();
        item.authorized = false;
        item.updatedAt = uint64(block.timestamp);
        authorizedCheckerCount--;
        emit CheckerRevoked(checker, item.updatedAt);
    }

    function proposeConfiguration(uint256 writeFee, uint16 thresholdBps, uint8 adverseQuorum, uint8 verifyQuorum)
        external
        onlyAdmin
    {
        if (writeFee > MAX_WRITE_FEE) revert InvalidInput();
        if (thresholdBps < MIN_THRESHOLD_BPS || thresholdBps > BPS) revert InvalidInput();
        if (adverseQuorum < MIN_QUORUM || verifyQuorum < MIN_QUORUM) revert InvalidInput();
        uint64 eta = uint64(block.timestamp + TIMELOCK_DELAY);
        pendingConfig = PendingConfig({
            writeFee: writeFee,
            thresholdBps: thresholdBps,
            adverseQuorum: adverseQuorum,
            verificationQuorum: verifyQuorum,
            eta: eta
        });
        emit ConfigurationProposed(writeFee, thresholdBps, adverseQuorum, verifyQuorum, eta);
    }

    function cancelConfiguration() external onlyAdmin {
        if (pendingConfig.eta == 0) revert NothingPending();
        delete pendingConfig;
        emit ConfigurationCancelled();
    }

    function commitConfiguration() external onlyAdmin {
        PendingConfig memory pending = pendingConfig;
        if (pending.eta == 0) revert NothingPending();
        if (block.timestamp < pending.eta) revert TimelockPending();
        endpointWriteFee = pending.writeFee;
        reliabilityThresholdBps = pending.thresholdBps;
        adverseReportQuorum = pending.adverseQuorum;
        verificationQuorum = pending.verificationQuorum;
        delete pendingConfig;
        emit ConfigurationUpdated(pending.writeFee, pending.thresholdBps, pending.adverseQuorum, pending.verificationQuorum);
    }

    function proposeFeeRecipient(address payable newRecipient) external onlyAdmin {
        if (newRecipient == address(0)) revert InvalidInput();
        pendingFeeRecipient = newRecipient;
        feeRecipientEta = uint64(block.timestamp + TIMELOCK_DELAY);
        emit FeeRecipientProposed(newRecipient, feeRecipientEta);
    }

    function commitFeeRecipient() external onlyAdmin {
        if (pendingFeeRecipient == address(0)) revert NothingPending();
        if (block.timestamp < feeRecipientEta) revert TimelockPending();
        emit FeeRecipientUpdated(feeRecipient, pendingFeeRecipient);
        feeRecipient = pendingFeeRecipient;
        pendingFeeRecipient = payable(address(0));
        feeRecipientEta = 0;
    }

    function transferAdmin(address newAdmin) external onlyAdmin {
        if (newAdmin == address(0)) revert InvalidInput();
        pendingAdmin = newAdmin;
        emit AdminTransferStarted(admin, newAdmin);
    }

    function acceptAdmin() external {
        if (msg.sender != pendingAdmin) revert Unauthorized();
        emit AdminTransferred(admin, msg.sender);
        admin = msg.sender;
        pendingAdmin = address(0);
    }

    // -------------------------------------------------------------- treasury

    /// @notice Fees accrue in the contract and are pulled to the fee recipient.
    ///         No value transfer ever happens on an operator's write path, so a
    ///         recipient that cannot accept a transfer can never block the
    ///         registry. Permissionless: anyone may trigger the sweep, but the
    ///         destination is always the configured recipient.
    function withdrawFees() external nonReentrant {
        uint256 amount = address(this).balance;
        if (amount == 0) revert NothingPending();
        (bool ok,) = feeRecipient.call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit FeesWithdrawn(feeRecipient, amount);
    }

    // ----------------------------------------------------------------- views

    function getInstance(uint64 instanceId) external view returns (Instance memory) {
        return _instances[instanceId];
    }

    function getEndpoint(uint64 endpointId) external view returns (Endpoint memory) {
        return _endpoints[endpointId];
    }

    function getLatestAggregate(uint64 endpointId) external view returns (DayAggregate memory) {
        return _latestAggregate[endpointId];
    }

    function getChecker(address checker) external view returns (Checker memory) {
        return _checkers[checker];
    }

    function getCheckerAddresses() external view returns (address[] memory) {
        return _checkerAddresses;
    }

    /// @notice Timelock deadline for a proposed checker authorization, 0 if none.
    ///         The proposed name and telemetry URL are in the proposal event.
    function checkerAuthEta(address checker) external view returns (uint64) {
        return _pendingCheckerAuth[checker].eta;
    }

    function getOwnerInstanceIds(address owner) external view returns (uint64[] memory) {
        return _ownerInstanceIds[owner];
    }

    function getInstanceEndpointIds(uint64 instanceId) external view returns (uint64[] memory) {
        return _instanceEndpointIds[instanceId];
    }

    function activeInstanceCount() external view returns (uint256) {
        return _activeInstanceIds.length;
    }

    function verificationCount(uint64 endpointId, uint32 epoch) external view returns (uint16) {
        return _verificationCount[endpointId][epoch];
    }

    /// @notice Page through the ids of live instances. Deleted instances are
    ///         swap-removed from this list, so a page never wastes slots on them.
    ///         `atRevision` lets a client detect a write that landed mid-read.
    function activeInstanceIds(uint256 cursor, uint256 limit)
        external
        view
        returns (uint64[] memory ids, uint256 nextCursor, uint64 atRevision)
    {
        uint256 total = _activeInstanceIds.length;
        if (cursor >= total || limit == 0) return (new uint64[](0), total, revision);
        uint256 end = cursor + limit;
        if (end < cursor || end > total) end = total;
        ids = new uint64[](end - cursor);
        for (uint256 i = cursor; i < end; ++i) ids[i - cursor] = _activeInstanceIds[i];
        nextCursor = end;
        atRevision = revision;
    }

    // -------------------------------------------------------------- internals

    function _applyReport(Attestation calldata report, uint32 day) internal returns (bool) {
        Endpoint storage endpoint = _endpoints[report.endpointId];
        if (!endpoint.active) return false;
        if (report.epoch != endpoint.epoch) return false; // measurement is stale
        if (report.uptimeBps > BPS || report.errorBps > BPS) return false;

        bool adverse = report.uptimeBps < reliabilityThresholdBps;
        // Healthy reports are only meaningful as recovery evidence.
        if (!adverse && !endpoint.unreliable) return false;

        CheckerReport storage previous = _checkerLatestReport[report.endpointId][msg.sender];
        if (previous.day == day && previous.epoch == report.epoch) return false; // already counted today
        previous.day = day;
        previous.epoch = report.epoch;
        previous.uptimeBps = report.uptimeBps;
        previous.latencyP95Ms = report.latencyP95Ms;
        previous.errorBps = report.errorBps;

        _aggregate(report, day, adverse);
        emit ReportSubmitted(report.endpointId, msg.sender, day, report.epoch, report.uptimeBps, report.latencyP95Ms, report.errorBps);
        _settleReliability(endpoint, day);
        return true;
    }

    function _aggregate(Attestation calldata report, uint32 day, bool adverse) internal {
        DayAggregate storage aggregate = _latestAggregate[report.endpointId];
        if (aggregate.day != day || aggregate.epoch != report.epoch) {
            aggregate.day = day;
            aggregate.epoch = report.epoch;
            aggregate.reportCount = 1;
            aggregate.adverseCount = adverse ? 1 : 0;
            aggregate.healthyCount = adverse ? 0 : 1;
            aggregate.uptimeBpsAverage = report.uptimeBps;
            aggregate.latencyP95AverageMs = report.latencyP95Ms;
            aggregate.errorBpsAverage = report.errorBps;
        } else {
            uint256 count = aggregate.reportCount;
            aggregate.uptimeBpsAverage = uint16((uint256(aggregate.uptimeBpsAverage) * count + report.uptimeBps) / (count + 1));
            aggregate.latencyP95AverageMs = uint32((uint256(aggregate.latencyP95AverageMs) * count + report.latencyP95Ms) / (count + 1));
            aggregate.errorBpsAverage = uint32((uint256(aggregate.errorBpsAverage) * count + report.errorBps) / (count + 1));
            aggregate.reportCount++;
            if (adverse) aggregate.adverseCount++;
            else aggregate.healthyCount++;
        }
        aggregate.lastReportedAt = uint64(block.timestamp);
    }

    function _settleReliability(Endpoint storage endpoint, uint32 day) internal {
        DayAggregate storage aggregate = _latestAggregate[endpoint.id];
        uint64 now64 = uint64(block.timestamp);
        if (!endpoint.unreliable) {
            if (aggregate.adverseCount >= adverseReportQuorum) {
                endpoint.unreliable = true;
                endpoint.updatedAt = now64;
                if (endpoint.lastAdverseDay != day) {
                    endpoint.lastAdverseDay = day;
                    endpoint.adverseDayCount++;
                }
                emit EndpointReliabilityChanged(endpoint.id, true, endpoint.epoch, now64);
            }
        } else if (aggregate.healthyCount >= adverseReportQuorum && aggregate.adverseCount == 0) {
            // Independent quorum agrees the endpoint is back at target.
            endpoint.unreliable = false;
            endpoint.updatedAt = now64;
            emit EndpointReliabilityChanged(endpoint.id, false, endpoint.epoch, now64);
        }
    }

    function _applyVerification(uint64 endpointId, uint32 epoch) internal {
        Endpoint storage endpoint = _endpoints[endpointId];
        if (!endpoint.active || endpoint.epoch != epoch) return;
        if (_verifiedBy[endpointId][epoch][msg.sender]) return;
        _verifiedBy[endpointId][epoch][msg.sender] = true;
        uint16 count = _verificationCount[endpointId][epoch] + 1;
        _verificationCount[endpointId][epoch] = count;
        if (!endpoint.verified && count >= verificationQuorum) {
            endpoint.verified = true;
            emit EndpointVerified(endpointId, epoch, count, uint64(block.timestamp));
        }
    }

    function _addEndpoint(uint64 instanceId, EndpointKind kind, string calldata url, uint64 now64)
        internal
        returns (uint64 endpointId)
    {
        _validateUrl(url);
        endpointId = nextEndpointId++;
        Endpoint storage endpoint = _endpoints[endpointId];
        endpoint.id = endpointId;
        endpoint.instanceId = instanceId;
        endpoint.addedAt = now64;
        endpoint.updatedAt = now64;
        endpoint.epoch = 1;
        endpoint.kind = uint8(kind);
        endpoint.active = true;
        endpoint.fingerprint = keccak256(abi.encodePacked(uint8(kind), url));
        endpoint.url = url;
        _instanceEndpointIds[instanceId].push(endpointId);
        _instances[instanceId].endpointCount++;
        emit EndpointAdded(endpointId, instanceId, uint8(kind), url, 1, now64);
    }

    function _removeActiveInstance(uint64 instanceId) internal {
        uint256 index = _activeInstanceIndex[instanceId];
        if (index == 0) return;
        uint256 last = _activeInstanceIds.length;
        if (index != last) {
            uint64 moved = _activeInstanceIds[last - 1];
            _activeInstanceIds[index - 1] = moved;
            _activeInstanceIndex[moved] = index;
        }
        _activeInstanceIds.pop();
        delete _activeInstanceIndex[instanceId];
    }

    function _countActiveEndpoints(uint64 instanceId) internal view returns (uint16 count) {
        uint64[] storage ids = _instanceEndpointIds[instanceId];
        uint256 length = ids.length;
        for (uint256 i; i < length; ++i) {
            if (_endpoints[ids[i]].active) count++;
        }
    }

    function _ownedActiveInstance(uint64 instanceId) internal view returns (Instance storage instance) {
        instance = _instances[instanceId];
        if (instance.owner != msg.sender) revert Unauthorized();
        if (!instance.active) revert NotFound();
    }

    function _ownedActiveEndpoint(uint64 endpointId) internal view returns (Endpoint storage endpoint) {
        endpoint = _endpoints[endpointId];
        if (!endpoint.active) revert NotFound();
        if (_instances[endpoint.instanceId].owner != msg.sender) revert Unauthorized();
    }

    function _validateMetadata(string calldata name, string calldata region, string calldata operatorInfo)
        internal
        pure
    {
        uint256 nameLength = bytes(name).length;
        if (nameLength == 0 || nameLength > MAX_NAME_BYTES) revert InvalidInput();
        if (bytes(region).length > MAX_NAME_BYTES) revert InvalidInput();
        if (bytes(operatorInfo).length > MAX_INFO_BYTES) revert InvalidInput();
    }

    /// @dev Accepts http(s), ws(s) and grpc(s) origins. Rejects control characters,
    ///      whitespace, embedded credentials and anything that would let a record
    ///      break out of its own field when rendered or probed.
    function _validateUrl(string calldata url) internal pure {
        bytes calldata value = bytes(url);
        uint256 length = value.length;
        if (length < 8 || length > MAX_URL_BYTES) revert InvalidInput();

        uint256 offset = _schemeLength(value);
        if (offset == 0 || length <= offset) revert InvalidInput();
        if (uint8(value[offset]) == 0x2F) revert InvalidInput(); // empty authority

        for (uint256 i = offset; i < length; ++i) {
            uint8 char = uint8(value[i]);
            // Printable ASCII only, and nothing that could break out of a rendered
            // attribute, a shell argument, or the URL authority itself.
            if (char <= 0x20 || char >= 0x7F) revert InvalidInput();
            if (char == 0x22 || char == 0x27 || char == 0x3C || char == 0x3E) revert InvalidInput();
            if (char == 0x5C || char == 0x40 || char == 0x60) revert InvalidInput();
        }
    }

    function _schemeLength(bytes calldata value) internal pure returns (uint256) {
        bytes32 head = keccak256(value[0:8]);
        if (head == _HTTPS || head == _GRPCS) return 8;
        head = keccak256(value[0:7]);
        if (head == _HTTP || head == _GRPC) return 7;
        if (keccak256(value[0:6]) == _WSS) return 6;
        if (keccak256(value[0:5]) == _WS) return 5;
        return 0;
    }

    function _collectFee(uint256 units) internal {
        if (msg.value != endpointWriteFee * units) revert IncorrectFee();
    }
}
