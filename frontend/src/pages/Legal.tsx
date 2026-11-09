// The privacy policy and terms. DRAFTS: the operator reviews and adapts them
// before the service goes public; they aren't legal advice. Every statement
// about data describes what the code does today. Keep the two in sync when
// that changes. OPERATOR_NAME and CONTACT_EMAIL fill in who runs the service.
import { useQuery } from "@tanstack/react-query";
import { Card, Layout, Typography, theme } from "antd";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Brand } from "../components/Brand";
import { fmtDate } from "../format";

const UPDATED = "2026-11-08";

function useOperator() {
  const info = useQuery({ queryKey: ["info"], queryFn: api.info, staleTime: Infinity }).data;
  const who = info?.operator_name || "the operator of this service";
  const contact = info?.contact_email ? (
    <a href={`mailto:${info.contact_email}`}>{info.contact_email}</a>
  ) : (
    "the contact address the operator publishes"
  );
  return { who, contact };
}

function LegalShell({ title, children }: { title: string; children: ReactNode }) {
  const { token } = theme.useToken();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header style={{ display: "flex", alignItems: "center", borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Brand />
      </Layout.Header>
      <Layout.Content style={{ padding: "24px 16px", maxWidth: 820, margin: "0 auto", width: "100%" }}>
        <Card>
          <Typography>
            <Typography.Title level={2} style={{ marginTop: 0 }}>{title}</Typography.Title>
            <Typography.Paragraph type="secondary">Last updated {fmtDate(UPDATED)}</Typography.Paragraph>
            {children}
            <Typography.Paragraph>
              <Link to="/privacy">Privacy policy</Link> · <Link to="/terms">Terms</Link> · <Link to="/">Back to Packages Tracker</Link>
            </Typography.Paragraph>
          </Typography>
        </Card>
      </Layout.Content>
    </Layout>
  );
}

const { Title, Paragraph } = Typography;

export function PrivacyPage() {
  const { who, contact } = useOperator();
  return (
    <LegalShell title="Privacy policy">
      <Paragraph>
        Packages Tracker is run by {who}. It finds the dependencies in your repositories that have reached, or will
        soon reach, end of life. This page says what it keeps about you, why, and who else sees any of it. Questions
        and requests: {contact}.
      </Paragraph>

      <Title level={4}>What we keep</Title>
      <ul>
        <li>
          <strong>Your GitHub profile</strong>: your GitHub user id, username, display name and avatar address. We
          don't ask GitHub for your email address.
        </li>
        <li>
          <strong>A GitHub sign-in token</strong>, stored encrypted. It has no extra permissions: it reads public
          data only, and it's used for GitHub lookups during your repositories' scans so they count against your own
          GitHub rate limit.
        </li>
        <li>
          <strong>Sessions</strong>: a fingerprint (hash) of each session's cookie, when it was used, and the browser's
          user-agent string.
        </li>
        <li>
          <strong>What you add</strong>: repository addresses and branches, uploaded zip files (kept so they can be
          scanned again), workspace settings, and the API keys and tokens you save. Keys and tokens are stored
          encrypted and are never shown again in full.
        </li>
        <li>
          <strong>Scan results</strong>: the packages found, their end-of-life findings and sources, and a log of each
          scan. Repositories are cloned only for the duration of a scan and deleted after it.
        </li>
        <li>
          <strong>Usage records</strong>: the days you used the service, which features were used (for example a CSV
          export or a manual scan), and facts about each scan (its timing, outcome, and AI usage such as tokens and
          searches). We don't store IP addresses.
        </li>
        <li>
          <strong>An audit log</strong> for each workspace, of who changed what (repositories, settings, members,
          keys). It never contains key values. A workspace's Admins and Owners can read it.
        </li>
      </ul>

      <Title level={4}>Free scans without an account</Title>
      <Paragraph>
        You can scan one repository without signing in. We keep its address, the scan's results and a cookie that
        lets your browser see them, for a day. If you sign in with GitHub in that time, the repository and its results
        move into your account; otherwise they're deleted. A token you give for a private repository is used for that
        one scan and never stored. To limit free scans per network, we count them by IP address in the server's
        memory, for a day; IP addresses are never written down.
      </Paragraph>

      <Title level={4}>Who else sees it</Title>
      <ul>
        <li>
          <strong>Members of your workspaces</strong> see that workspace's repositories, results and members.
        </li>
        <li>
          <strong>GitHub, GitLab and other git hosts</strong>, when we sign you in or clone your repositories with the
          tokens you provide.
        </li>
        <li>
          <strong>Public package sources</strong> (such as package registries and endoflife.date), which receive the
          names and versions of the packages being checked.
        </li>
        <li>
          <strong>The AI and search providers your workspace chooses</strong>, only when AI research is on. They
          receive package names and versions and searches about them, sent with your workspace's own key under that
          provider's terms. Some free tiers (Gemini's, for example) let the provider use what it receives to improve
          its products. Your source code is never sent.
        </li>
        <li>
          <strong>The operator</strong> can see accounts and workspace metadata (names, members, repository
          addresses) to run the service, but not scan results or keys. When the operator opens a workspace's details,
          that's recorded in the workspace's audit log.
        </li>
      </ul>
      <Paragraph>We don't sell data, show ads, or use tracking cookies.</Paragraph>

      <Title level={4}>Cookies and browser storage</Title>
      <Paragraph>
        One cookie keeps you signed in (or, for a free scan, lets you see it), and two short-lived ones protect the
        sign-in itself. The browser also remembers your light or dark theme and which workspace you last used.
      </Paragraph>

      <Title level={4}>How long we keep it, and deleting it</Title>
      <Paragraph>
        Data is kept until you delete it. Deleting a repository deletes its scan history and uploads. Deleting your
        account (from the account menu) deletes your profile, sessions and usage records, your personal workspace
        with everything in it, and any team workspace where you're the only member. In team workspaces you share,
        audit entries about your actions stay, attributed to a deleted user. Copies in backups disappear as the
        backups expire.
      </Paragraph>

      <Title level={4}>Changes</Title>
      <Paragraph>If this policy changes, this page will show the new version and its date.</Paragraph>
    </LegalShell>
  );
}

export function TermsPage() {
  const { who, contact } = useOperator();
  return (
    <LegalShell title="Terms of use">
      <Paragraph>
        These terms apply to Packages Tracker, run by {who}. By signing in you agree to them. Contact: {contact}.
      </Paragraph>

      <Title level={4}>The service</Title>
      <Paragraph>
        The service is free and provided as it is, without guarantees of availability or accuracy. It may change,
        limit usage, or stop at any time. Sign-ups may be capped.
      </Paragraph>

      <Title level={4}>Results are guidance</Title>
      <Paragraph>
        End-of-life findings come from public sources and, when you turn it on, AI research. They can be incomplete,
        out of date, or wrong. Check the cited sources before you rely on a finding.
      </Paragraph>

      <Title level={4}>Your responsibilities</Title>
      <ul>
        <li>Add only repositories and files you're allowed to share with this service.</li>
        <li>
          Keys you add stay yours: what the AI and search providers charge for the research your workspace runs is
          between you and them, under their terms.
        </li>
        <li>
          Don't try to disrupt or overload the service, get around its limits, or reach other people's data, and
          don't upload anything unlawful or harmful.
        </li>
      </ul>

      <Title level={4}>Your content</Title>
      <Paragraph>
        What you add stays yours. You allow the service to store and process it only to run the service for you and
        the members of your workspaces.
      </Paragraph>

      <Title level={4}>Accounts</Title>
      <Paragraph>
        You sign in with your GitHub account. The operator may disable an account that breaks these terms or puts the
        service at risk. You can delete your account at any time from the account menu.
      </Paragraph>

      <Title level={4}>Liability</Title>
      <Paragraph>
        As far as the law allows, the operator isn't liable for losses that come from using the service or relying on
        its results.
      </Paragraph>

      <Title level={4}>Changes</Title>
      <Paragraph>
        If these terms change, this page will show the new version and its date. Using the service after that means
        you accept them.
      </Paragraph>
    </LegalShell>
  );
}
