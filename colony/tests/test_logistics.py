"""Battery, charging and supply kits (§3.3, §5.1 lane 2).

§3.3 gives every role a battery quoted in ticks and gives the medic two kits.
Neither meant anything until now: a robot could fly forever and stabilize the
whole map from one satchel. With both enforced, knowing when to break off is
part of the job — and a robot that gets it wrong is stranded in a disaster zone
holding a task nobody else can see it has abandoned.
"""

from agents import logistics
from agents.scout import Scout
from agents.worker import Worker
from sim.protocol import Action
from sim.world import MEDIC_KITS, RECHARGE_TICKS, ROLES, World
from world.map_format import EMPTY, parse_map


def _world(width=20, height=20, spawn=None, victims=(), zones=None):
    return World(
        parse_map(
            {
                "width": width,
                "height": height,
                "tile_size": 32,
                "layers": {
                    "ground": [["open"] * width for _ in range(height)],
                    "objects": [[EMPTY] * width for _ in range(height)],
                },
                "zones": zones if zones is not None else [],
                "spawn_points": spawn or {},
                "victims": list(victims),
                "escalations": [],
            }
        ),
        seed=0,
    )


def _run(world, agents, ticks):
    for _ in range(ticks):
        world.step({a.robot_id: a.step(world) for a in agents})


# --- the sim's half ----------------------------------------------------------


def test_a_flat_battery_strands_a_robot():
    """Not a soft nudge: §3.3's battery is a constraint or it is decoration, and
    the agents' return-to-base logic is only worth writing if running out is
    actually terminal."""
    world = _world(spawn={"medic": [{"x": 5, "y": 5}]})
    world.robots["m1"].battery = 0

    world.step({"m1": Action.move("e")})

    assert (world.robots["m1"].x, world.robots["m1"].y) == (5, 5)
    assert world.robots["m1"].status == "stranded"


def test_recharging_at_base_refills_the_battery():
    world = _world(spawn={"medic": [{"x": 2, "y": 2}]})
    world.robots["m1"].battery = 5

    for _ in range(RECHARGE_TICKS):
        world.step({"m1": Action.act("recharge", (2, 2))})

    assert world.robots["m1"].battery == ROLES["medic"]["battery"]


def test_recharging_anywhere_else_is_refused():
    """The staging zone is the charger (§3.3). A robot that could top up in the
    field would never need to come home, and the whole logistics loop — and the
    lifter clearing a route back through the block — would be theatre."""
    world = _world(
        spawn={"medic": [{"x": 15, "y": 15}]},
        zones=[{"name": "staging", "x": 0, "y": 0, "width": 8, "height": 8}],
    )
    world.robots["m1"].battery = 5

    world.step({"m1": Action.act("recharge", (15, 15))})

    assert world.robots["m1"].battery == 5
    assert world.robots["m1"].status == "blocked"


def test_a_medic_spends_a_kit_per_victim():
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 6, "y": 5, "vitals_deadline": 700}],
    )
    for _ in range(3):
        world.step({"m1": Action.act("stabilize", (6, 5))})

    assert world.victims["v1"].state == "stabilized"
    assert world.robots["m1"].kits == MEDIC_KITS - 1


def test_a_medic_out_of_kits_cannot_stabilize():
    world = _world(
        spawn={"medic": [{"x": 5, "y": 5}]},
        victims=[{"id": "v1", "x": 6, "y": 5, "vitals_deadline": 700}],
    )
    world.robots["m1"].kits = 0

    world.step({"m1": Action.act("stabilize", (6, 5))})

    assert world.victims["v1"].state != "stabilized"


def test_restocking_refills_the_satchel():
    world = _world(spawn={"medic": [{"x": 2, "y": 2}]})
    world.robots["m1"].kits = 0

    for _ in range(3):
        world.step({"m1": Action.act("restock", (2, 2))})

    assert world.robots["m1"].kits == MEDIC_KITS


def test_only_a_medic_carries_kits():
    world = _world(spawn={"lifter": [{"x": 2, "y": 2}]})
    world.step({"l1": Action.act("restock", (2, 2))})
    assert world.robots["l1"].status == "blocked"


# --- the agents' half --------------------------------------------------------


def test_a_robot_heads_home_before_it_cannot_get_home(mem, mission):
    """The margin is the point. A robot that leaves at exactly enough battery
    arrives on empty, and any detour on the way strands it — in the middle of
    the block, holding a victim's delivery."""
    world = _world(spawn={"medic": [{"x": 15, "y": 15}]})
    robot = world.robots["m1"]
    robot.battery = logistics.ticks_home(world, "medic", (15, 15)) + 1

    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    medic.step(world)

    assert medic.homing


def test_a_robot_with_plenty_of_battery_gets_on_with_the_job(mem, mission):
    world = _world(spawn={"medic": [{"x": 15, "y": 15}]})
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    medic.step(world)
    assert not medic.homing


def test_a_returning_robot_gives_its_task_back(mem, mission):
    """§4.4's explicit release. Carrying the task home means renewing a lease on
    work that is not being done for as long as the round trip takes — the one
    thing leases exist to prevent."""
    world = _world(
        spawn={"medic": [{"x": 15, "y": 15}]},
        victims=[{"id": "v1", "x": 16, "y": 16, "vitals_deadline": 700}],
    )
    mem.register_victim(mission, (16, 16), reported_by="s1")
    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    medic.step(world)  # claims the delivery
    assert medic.task is not None

    world.robots["m1"].battery = 1
    medic.step(world)

    assert medic.task is None
    assert any(t.kind == "deliver_kit" for t in mem.open_tasks(mission))


def test_a_medic_out_of_kits_goes_to_restock(mem, mission):
    world = _world(spawn={"medic": [{"x": 5, "y": 5}]})
    world.robots["m1"].kits = 0

    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    medic.step(world)

    assert medic.homing


def test_a_robot_goes_home_charges_and_comes_back_to_work(mem, mission):
    """The whole loop, end to end: break off, charge, restock, and be available
    again — with no supervisor telling it to."""
    world = _world(
        spawn={"medic": [{"x": 6, "y": 2}]},
        zones=[{"name": "staging", "x": 0, "y": 0, "width": 8, "height": 8}],
    )
    world.robots["m1"].battery = 4
    world.robots["m1"].kits = 0

    medic = Worker(robot_id="m1", role="medic", mission_id=mission, mem=mem)
    _run(world, [medic], RECHARGE_TICKS + 20)

    assert world.robots["m1"].battery == ROLES["medic"]["battery"]
    assert world.robots["m1"].kits == MEDIC_KITS
    assert not medic.homing


def test_a_scout_flies_home_and_releases_its_sector(mem, mission):
    """A scout that charges while holding a sector takes 10x10 tiles off the
    board for the length of the charge, and no other scout may sweep them —
    sector claims inverted into the duplicate-effort problem they exist to
    solve."""
    world = _world(
        width=30,
        height=30,
        spawn={"scout": [{"x": 20, "y": 20}]},
        zones=[{"name": "staging", "x": 0, "y": 0, "width": 8, "height": 8}],
    )
    task = mem.create_task(mission, "explore_sector:A1", (20, 20))
    scout = Scout(robot_id="s1", mission_id=mission, mem=mem)
    scout.step(world)  # claims a sector
    assert scout.sector_task is not None

    world.robots["s1"].battery = 3
    scout.step(world)

    assert scout.sector_task is None
    assert task in {t.id for t in mem.open_tasks(mission)}
