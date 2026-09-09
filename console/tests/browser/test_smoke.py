"""The eight things a person can do with the console, driven in a real
browser against the real server.

Every assertion here is about what is ON THE SCREEN: the row that was
drawn, the refusal under the Next button, the modal that opened and then
went away, the phase that went red and what it says to do about it, the
button that stays disabled. Nothing reads the console's own model, and
nothing asserts an HTTP status: those are covered by the suites beside
this one, and a page can be wrong while every one of them is right.

What is stood in for, and what is not: see tests/browser/conftest.py. The
short of it is the aws CLI (a stub executable on PATH) and the deploy and
teardown scripts (fake engines that write frames a test chose). The
console, its API, the profile writer, the engine runner, the events file,
the prompt FIFO, the SSE stream and every line of the page are real.

Run:  cd console && python3 -m pytest tests/browser -q
      (the default `pytest tests -q` does not collect this directory)
"""
import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed: pip install playwright && playwright install chromium")
expect = sync_api.expect


# --------------------------------------------------------------- helpers
def go_to_screen(page, n):
    """Press Next and wait for screen n of the wizard to be the one showing."""
    page.click("#nextBtn")
    expect(page.locator('#wizard section[data-screen="%d"]' % n)).to_be_visible()


def launch_the_default_plan(page):
    """Walk the wizard's six screens on its defaults and press Launch: what
    an operator does who accepts every answer. Leaves the page on Watch,
    following the run the fake engine is producing."""
    page.goto("/")
    for n in (2, 3, 4, 5, 6):
        go_to_screen(page, n)
    expect(page.locator("#launchBtn")).to_be_enabled()
    page.click("#launchBtn")
    expect(page.locator("#page-watch")).to_be_visible()


def phase_row(page, name):
    """The timeline row for one phase, by the name printed on it."""
    return page.locator('#wPhases li:has(.tlname:text-is("%s"))' % name)


def a_vpc(vpc_id="vpc-0aa11bb22cc33dd4", name="lab", cidr="10.0.0.0/16"):
    return {"Vpcs": [{"VpcId": vpc_id, "CidrBlock": cidr, "Tags": [{"Key": "Name", "Value": name}]}]}


def two_subnets(vpc_id="vpc-0aa11bb22cc33dd4"):
    return {"Subnets": [
        {"SubnetId": "subnet-0aaa1111bbb2222c", "AvailabilityZone": "us-east-1a", "CidrBlock": "10.0.1.0/24",
         "MapPublicIpOnLaunch": True, "VpcId": vpc_id, "Tags": [{"Key": "Name", "Value": "public-a"}]},
        {"SubnetId": "subnet-0ddd3333eee4444f", "AvailabilityZone": "us-east-1a", "CidrBlock": "10.0.2.0/24",
         "MapPublicIpOnLaunch": False, "VpcId": vpc_id, "Tags": [{"Key": "Name", "Value": "private-a"}]}]}


def instances(*rows):
    """describe-instances as the CLI returns it: one reservation per row."""
    return {"Reservations": [{"Instances": [r]} for r in rows]}


def an_instance(instance_id, name, platform="Linux/UNIX", ip="10.0.1.20"):
    return {"InstanceId": instance_id, "InstanceType": "t3.micro", "VpcId": "vpc-0aa11bb22cc33dd4",
            "SubnetId": "subnet-0aaa1111bbb2222c", "Placement": {"AvailabilityZone": "us-east-1a"},
            "State": {"Name": "running"}, "PlatformDetails": platform, "PrivateIpAddress": ip,
            "Tags": [{"Key": "Name", "Value": name}, {"Key": "cloudlens", "Value": "yes"}]}


# 1 ------------------------------------------------------------------------
def test_the_wizard_reaches_the_plan_and_the_profile_says_the_vpc_is_new(page):
    """Six screens of defaults, and the profile the script would replay is
    on the last one with CLOUDLENS_INFRA="new" in it."""
    page.goto("/")
    expect(page.locator('#wizard section[data-screen="1"]')).to_be_visible()
    expect(page.locator("#wStack")).to_have_value("cloudlens-stack")
    expect(page.locator("#wRegion")).to_have_value("us-east-1")
    expect(page.locator("#infraNew")).to_be_checked()

    for n in (2, 3, 4, 5, 6):
        go_to_screen(page, n)

    expect(page.locator("#planStatus")).to_contain_text("Stack cloudlens-stack in us-east-1")
    expect(page.locator("#profileName")).to_have_text("deploy-profile-cloudlens-stack.env")

    # the profile is behind a fold, as a person opens it
    page.click("#page-deploy summary:has-text('Profile text')")
    profile = page.locator("#profileText")
    expect(profile).to_be_visible()
    expect(profile).to_contain_text('CLOUDLENS_INFRA="new"')
    expect(profile).to_contain_text('CLOUDLENS_STACK_NAME="cloudlens-stack"')
    expect(profile).to_contain_text('CLOUDLENS_REGION="us-east-1"')

    # and the line that does the same thing from a terminal names that file
    expect(page.locator("#cliLine")).to_contain_text(
        "bash deploy/deploy-stack.sh --profile deploy-profile-cloudlens-stack.env")


# 2 ------------------------------------------------------------------------
def test_an_existing_vpc_plan_will_not_move_on_without_a_subnet(page, aws):
    """Choosing an existing VPC lists the account's VPCs and then its
    subnets. Next refuses, in words, until a management subnet is picked."""
    aws({"ec2 describe-vpcs": a_vpc(),
         "ec2 describe-subnets": two_subnets(),
         "ec2 describe-route-tables": {"RouteTables": []}})
    page.goto("/")
    page.check("#infraExisting")

    expect(page.locator("#vpcStatus")).to_contain_text("1 VPC in us-east-1")
    page.click('#vpcRows button.pickb:text-is("vpc-0aa11bb22cc33dd4")')
    expect(page.locator("#subnetStatus")).to_contain_text("2 subnets in vpc-0aa11bb22cc33dd4")
    expect(page.locator("#s1Pick")).to_contain_text("management subnet not chosen")

    # Next, with no subnet: the wizard says why and stays where it is
    page.click("#nextBtn")
    expect(page.locator("#wErr")).to_contain_text("Pick the management subnet")
    expect(page.locator('#wizard section[data-screen="1"]')).to_be_visible()
    expect(page.locator('#wizard section[data-screen="2"]')).to_be_hidden()

    # with one, it moves on, and the refusal is gone
    page.click('#subnetRows button.pickb:text-is("subnet-0aaa1111bbb2222c")')
    expect(page.locator("#s1Pick")).to_contain_text("management subnet subnet-0aaa1111bbb2222c")
    go_to_screen(page, 2)
    expect(page.locator("#wErr")).to_have_text("")


# 3 ------------------------------------------------------------------------
def test_the_workloads_screen_counts_the_instances_the_account_answers_with(page, aws):
    """The workloads screen asks the account which running instances carry
    the discovery tag, and shows the count and the rows it got back."""
    aws({"ec2 describe-vpcs": a_vpc(),
         "ec2 describe-instances": instances(
             an_instance("i-0aaa111bbb222ccc1", "web-1"),
             an_instance("i-0aaa111bbb222ccc2", "web-2", ip="10.0.1.21"),
             an_instance("i-0aaa111bbb222ccc3", "win-1", platform="Windows", ip="10.0.1.22"))})
    page.goto("/")
    for n in (2, 3, 4):
        go_to_screen(page, n)

    page.check("#wlExisting")
    expect(page.locator("#wlStatus")).to_contain_text("3 running instances match cloudlens=yes")
    expect(page.locator("#wlRows tr")).to_have_count(3)
    expect(page.locator("#wlRows")).to_contain_text("i-0aaa111bbb222ccc1")
    expect(page.locator("#wlRows")).to_contain_text("win-1")
    expect(page.locator("#wlRows")).to_contain_text("Windows")

    # the console really shelled out to the CLI for it, with the tag filter
    described = [c for c in aws.calls() if c.startswith("ec2 describe-instances")]
    assert described, aws.calls()
    assert "tag:cloudlens" in described[-1] and "--region us-east-1" in described[-1], described[-1]


# 4 ------------------------------------------------------------------------
# The order below is a subset of deploy-stack.sh's own PHASE_ORDER
# ("stack wait bootstrap key license adopt sensors eks vpb mirror path
# prove"), in that order, so the fixture is something the engine could
# actually emit. It was "preflight key stack wait vpb prove", which the
# script could not: preflight is not one of its phases at all, and the
# three it does have were in an order it never sends them in.
LAUNCH_EVENTS = [
    {"type": "hello", "stack": "cloudlens-stack", "region": "us-east-1", "dry_run": "false"},
    {"type": "phases", "order": "stack wait bootstrap key vpb prove"},
    {"type": "phase", "name": "stack", "status": "done", "reason": "CREATE_COMPLETE"},
    {"type": "resource", "kind": "vcontroller", "id": "i-0aaa111bbb222ccc9", "ip": "203.0.113.10"},
    {"type": "login", "component": "vcontroller", "url": "https://203.0.113.10/cloudlens/login",
     "user": "admin", "password_in": "~/.cloudlens-vcontroller-creds.json"},
    {"type": "phase", "name": "wait", "status": "skipped", "reason": "no vController wait was asked for"},
    {"type": "phase", "name": "bootstrap", "status": "done", "reason": "the account answered"},
    {"type": "phase", "name": "key", "status": "done", "reason": "key pair cloudlens-stack-key created"},
    {"type": "done", "status": "ok", "profile": "deploy-profile-cloudlens-stack.env"},
]


def test_launch_starts_a_run_and_the_watch_screen_draws_its_timeline(page, engine):
    """Launch on the plan screen posts the plan, the page moves to Watch,
    and every row of the timeline is a frame the run wrote."""
    engine(LAUNCH_EVENTS)
    launch_the_default_plan(page)
    expect(page.locator("#wChip")).to_contain_text("cloudlens-stack")

    # The phase list the run sent, in the order the RUN sent it, which is
    # what the assertion is written against and not deploy-stack.sh's
    # PHASE_ORDER. The page's job is to draw the list it was handed; a page
    # that drew its own idea of the order instead would still pass an
    # assertion copied from the script, and be wrong. The fixture is a real
    # subset in the script's order (see above) so the two agree here, but
    # it is the fixture that is being checked.
    expect(page.locator("#wPhases li")).to_have_count(6)
    expect(page.locator("#wPhases .tlname")).to_have_text(
        ["stack", "wait", "bootstrap", "key", "vpb", "prove"])
    expect(phase_row(page, "stack")).to_have_class("tlrow done")
    expect(phase_row(page, "key")).to_contain_text("key pair cloudlens-stack-key created")
    expect(phase_row(page, "wait")).to_have_class("tlrow skipped")
    expect(phase_row(page, "vpb")).to_have_class("tlrow pending")

    # what the run reported about itself, beside the timeline
    expect(page.locator("#wTopo")).to_contain_text("203.0.113.10")
    expect(page.locator("#wLogins")).to_contain_text("https://203.0.113.10/cloudlens/login")
    expect(page.locator("#wLogins")).to_contain_text("~/.cloudlens-vcontroller-creds.json")

    banner = page.locator("#wBanner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Run complete")
    expect(banner).to_contain_text("deploy-profile-cloudlens-stack.env")
    expect(page.locator("#wPillTxt")).to_have_text("complete")


# 5 ------------------------------------------------------------------------
PROMPT_EVENTS = [
    {"type": "hello", "stack": "cloudlens-stack", "region": "us-east-1"},
    {"type": "phases", "order": "stack key"},
    {"type": "prompt", "id": "p1", "question": "EC2 key pair to use?", "default": "cloudlens-stack-key",
     "kind": "text"},
    {"type": "phase", "name": "key", "status": "done", "reason": "using lab-key"},
    {"type": "done", "status": "ok"},
]


def test_a_question_opens_the_modal_and_answering_it_clears_the_modal(page, engine):
    """A prompt frame stops the page on a modal; the answer goes down the
    FIFO to the engine, the modal goes away, and the question is on the
    screen afterwards with what was sent."""
    engine(PROMPT_EVENTS)
    launch_the_default_plan(page)

    modal = page.locator("#wPrompt")
    expect(modal).to_be_visible()
    expect(page.locator("#wPromptQ")).to_have_text("EC2 key pair to use?")
    expect(page.locator("#wPromptHint")).to_contain_text("Enter alone takes the default: cloudlens-stack-key")

    page.fill("#wPromptInput", "lab-key")
    page.click("#wPromptSend")

    expect(modal).to_be_hidden()
    expect(page.locator("#wQuestions")).to_contain_text("EC2 key pair to use?")
    expect(page.locator("#wQuestions")).to_contain_text("lab-key")

    # the engine on the other end of the FIFO got those bytes, and said so
    page.click("#page-watch summary:has-text('Raw output')")
    expect(page.locator("#wLog")).to_contain_text("engine read the answer: lab-key")
    expect(page.locator("#wBanner")).to_contain_text("Run complete")


# 6 ------------------------------------------------------------------------
FAILURE = ("no EC2 key pair named lab-key in us-east-1: create it, or leave the key field empty "
           "and the run will list yours and ask")
# a real subset of deploy-stack.sh's PHASE_ORDER, in its order, as above
FAILED_EVENTS = [
    {"type": "hello", "stack": "cloudlens-stack", "region": "us-east-1"},
    {"type": "phases", "order": "stack key vpb"},
    {"type": "phase", "name": "stack", "status": "done", "reason": "CREATE_COMPLETE"},
    {"type": "phase", "name": "key", "status": "failed", "reason": FAILURE},
    {"type": "done", "status": "failed", "phase": "key", "reason": FAILURE, "code": "3"},
]


def accent_of(page):
    """The colour --accent resolves to on this page, in the rgb() form
    getComputedStyle answers with, read through a probe element because the
    token itself computes to the hex it was written as.

    Read rather than written down: what is under test is that a failed row
    takes the console's accent, and the shade the accent happens to be is
    a token in index.html that may be retuned. It was hardcoded as
    rgb(228, 0, 43), so retuning the palette would have failed this test
    without anything on the screen being wrong."""
    return page.evaluate("""() => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--accent)';
        document.body.appendChild(probe);
        const c = getComputedStyle(probe).color;
        probe.remove();
        return c;
    }""")


def test_a_failed_phase_turns_its_row_red_and_says_what_to_do_about_it(page, engine):
    """The phase that failed is red on the timeline, carries the script's
    own reason, and the banner repeats it with the phase and the exit
    code. The phases before and after it are untouched."""
    engine(FAILED_EVENTS, exit_code=3)
    launch_the_default_plan(page)
    accent = accent_of(page)

    row = phase_row(page, "key")
    expect(row).to_have_class("tlrow failed")
    expect(row.locator(".tlmark")).to_have_text("✕")
    expect(row.locator(".tlwhy")).to_have_text(FAILURE)
    assert row.locator(".tlname").evaluate("el => getComputedStyle(el).color") == accent
    assert row.locator(".tlmark").evaluate("el => getComputedStyle(el).color") == accent

    # the phase that passed is not red, and the one that never ran is pending
    assert phase_row(page, "stack").locator(".tlname").evaluate(
        "el => getComputedStyle(el).color") != accent
    expect(phase_row(page, "vpb")).to_have_class("tlrow pending")

    banner = page.locator("#wBanner")
    expect(banner).to_have_class("banner bad")
    expect(banner).to_contain_text("Run failed")
    expect(banner).to_contain_text("Ended as failed in phase key: " + FAILURE)
    expect(banner).to_contain_text("(exit 3)")
    expect(page.locator("#wPillTxt")).to_have_text("failed")


# 7 ------------------------------------------------------------------------
def test_the_teardown_stays_disarmed_until_the_stack_name_is_typed_back(page, aws, teardown_script):
    """The audit runs read-only and prints its report; the destructive
    button stays disabled, saying what it wants, until the stack's own
    name is typed back exactly."""
    aws({"ec2 describe-instances": instances(),
         "ec2 describe-traffic-mirror-sessions": {"TrafficMirrorSessions": []}})
    teardown_script(["orphaned volume vol-0aaa111bbb222ccc1 (8 GB, available)",
                     "no orphaned security groups"])
    page.goto("/")
    page.click("#tab-teardown")
    expect(page.locator("#page-teardown")).to_be_visible()

    page.fill("#tdStack", "cloudlens-stack")
    page.fill("#tdRegion", "us-east-1")
    expect(page.locator("#tdRun")).to_be_disabled()
    expect(page.locator("#tdRunNote")).to_contain_text("Run the audit first")

    page.click("#tdAudit")
    expect(page.locator("#tdAuditStatus")).to_contain_text("The audit finished")
    expect(page.locator("#tdReport")).to_contain_text("orphaned volume vol-0aaa111bbb222ccc1")
    expect(page.locator("#tdReport")).to_contain_text("--orphans")

    # and it ran where the fixtures promise every fake engine runs: the
    # test's own directory, never the checkout. api._launch hands both
    # scripts the same module global as their cwd, so the fake teardown
    # used to run in the repository while the fake deploy ran in tmp_path.
    # The script prints the directory it was started in; this is the only
    # assertion that can see it, and the destructive path is the one that
    # has to be seen.
    expect(page.locator("#tdReport")).to_contain_text("cwd: " + teardown_script.cwd)

    # audited, and still refused: nothing has been typed
    expect(page.locator("#tdRun")).to_be_disabled()
    expect(page.locator("#tdRunNote")).to_have_text("Type cloudlens-stack to arm the teardown.")

    # a name that is nearly right is not the name
    page.fill("#tdConfirm", "cloudlens-stac")
    expect(page.locator("#tdRun")).to_be_disabled()
    expect(page.locator("#tdRunNote")).to_have_text("Type cloudlens-stack to arm the teardown.")

    # the name, exactly, is what arms it
    page.fill("#tdConfirm", "cloudlens-stack")
    expect(page.locator("#tdRun")).to_be_enabled()


# 8 ------------------------------------------------------------------------
def test_a_code_the_kvo_does_not_recognise_is_a_row_and_not_an_error_page(page, kvo):
    """A lookup that came back with nothing is a row in the table saying
    so, with the code shown only by its last four characters. The screen
    is still the licensing screen."""
    page.goto("/")
    page.click("#tab-licensing")
    expect(page.locator("#page-licensing")).to_be_visible()
    expect(page.locator("#licCodes")).to_contain_text("Add the codes and press Check")

    page.fill("#licKvo", "10.0.0.11")
    page.fill("#licUser", "admin")
    page.fill("#licPass", "not-a-real-password")
    page.fill("#licEntry", "LAB-CODE-ABCD")
    page.click("#licAdd")
    expect(page.locator("#licList")).to_contain_text("****-ABCD")

    page.click("#licCheck")

    row = page.locator("#licCodes tr")
    expect(row).to_have_count(1)
    expect(row.locator("code")).to_have_text("****-ABCD")
    expect(row.locator("span.st")).to_have_text("NO")
    expect(row).to_contain_text("the KVO recognised nothing under this code")
    expect(row).to_contain_text("FAILED")
    expect(row).to_contain_text("nothing to activate")
    expect(page.locator("#licStatus")).to_contain_text("0 of 1 code recognised")

    # the screen is still the screen: a refused lookup is not a dead end
    expect(page.locator("#page-licensing")).to_be_visible()
    expect(page.locator("#opsNav")).to_be_visible()
    expect(page.locator("#licActivate")).to_be_disabled()
    # and the code itself is nowhere on the page, only its tail. The whole
    # served document, not the licensing section's inner_text(): a code
    # that leaked into a title, an aria-label, a data- attribute or an
    # input's value is a code on a shared screen just the same, and none of
    # those are text a person can select.
    assert "LAB-CODE-ABCD" not in page.content()
