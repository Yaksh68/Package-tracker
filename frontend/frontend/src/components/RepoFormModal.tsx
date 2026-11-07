import { BranchesOutlined, InboxOutlined } from "@ant-design/icons";
import {
  Alert,
  Checkbox,
  Col,
  Collapse,
  DatePicker,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Typography,
  Upload,
  type UploadFile,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  buildCreatePayload,
  buildUpdatePayload,
  buildUploadMeta,
  tagsToText,
  textToTags,
  type RepoFormValues,
} from "../lib/repoForm";
import type { Repo, RepoCreate, RepoUpdate, UploadRepoMeta } from "../types";

const { Text } = Typography;

interface Props {
  open: boolean;
  editing?: Repo | null;
  onCancel: () => void;
  onCreate: (body: RepoCreate) => void;
  onCreateUpload: (meta: UploadRepoMeta, file: File) => void;
  /** `upload`: a zip chosen while editing an upload repo, sent after the changes. */
  onUpdate: (id: string, body: RepoUpdate, upload?: { file: File; version?: string }) => void;
  confirmLoading?: boolean;
  /** Server error from the last save, shown inside the dialog. */
  error?: string | null;
}

type FormShape = Omit<RepoFormValues, "release_date"> & { release_date: Dayjs | null; zip?: UploadFile[] };

const zipFile = (list?: UploadFile[]) => list?.[0]?.originFileObj as File | undefined;

// Fields under "Advanced" — a validation error there opens the panel so it is seen.
const ADVANCED_FIELDS = ["ignored_packages", "image_tags_text", "token", "remove_token"];

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <Divider orientation="left" orientationMargin={0} plain style={{ margin: "4px 0 12px" }}>
      <Text strong>{children}</Text>
    </Divider>
  );
}

export function RepoFormModal({
  open, editing, onCancel, onCreate, onCreateUpload, onUpdate, confirmLoading, error,
}: Props) {
  const [form] = Form.useForm<FormShape>();
  const [addMode, setAddMode] = useState<"git" | "upload">("git");
  const isUpload = editing ? editing.source === "upload" : addMode === "upload";
  const removeToken = Form.useWatch("remove_token", form);
  const hasStoredToken = !!editing?.token_override;
  const ignoredWatch = Form.useWatch("ignored_packages", form);
  const tagsWatch = Form.useWatch("image_tags_text", form);

  const detected = editing?.detected_tag_vars ?? [];
  const missingTags = detected.filter((v) => !editing?.image_tags?.[v]);
  // "Advanced" starts open only when the last scan is waiting on image tags.
  const [advancedOpen, setAdvancedOpen] = useState<string[]>([]);
  useEffect(() => {
    if (open) setAdvancedOpen(missingTags.length > 0 ? ["advanced"] : []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  // The dialog's content is created fresh on every open (destroyOnHidden), so
  // the saved values go in as initialValues — setting them from an effect ran
  // before the form existed and left every field empty. Fields are listed one
  // by one: the API returns a stored token as "****", which must never be put
  // back into the form (saving it would overwrite the real token).
  const initialValues = useMemo(
    () =>
      editing
        ? {
            url: editing.url,
            branches: editing.branches,
            release_date: editing.release_date ? dayjs(editing.release_date) : null,
            interval_days: editing.interval_days,
            image_tags_text: tagsToText(editing.image_tags, editing.detected_tag_vars),
            token: "",
            remove_token: false,
            ignored_packages: editing.ignored_packages ?? [],
          }
        : {
            branches: ["main"],
            interval_days: 1,
            release_date: null,
            image_tags_text: "",
            token: "",
            ignored_packages: [],
          },
    [editing],
  );

  // One-line summary on the collapsed "Advanced" header, so nothing set there is hidden.
  const advancedSummary = (() => {
    const parts: string[] = [];
    const ignored = (ignoredWatch ?? initialValues.ignored_packages ?? []).length;
    if (ignored) parts.push(`${ignored} ignored`);
    let tags = 0;
    try {
      tags = Object.keys(textToTags(tagsWatch ?? initialValues.image_tags_text)).length;
    } catch {
      /* shown as a field error */
    }
    if (tags) parts.push(`${tags} image tag${tags === 1 ? "" : "s"}`);
    if (missingTags.length) parts.push(`${missingTags.length} image tag${missingTags.length === 1 ? "" : "s"} missing`);
    if (!isUpload && hasStoredToken && !removeToken) parts.push("token saved");
    return parts.join(" · ");
  })();

  const submit = () =>
    form
      .validateFields()
      .then((raw) => {
        const v: RepoFormValues = {
          ...raw,
          release_date: raw.release_date ? dayjs(raw.release_date).format("YYYY-MM-DD") : null,
        };
        const file = zipFile(raw.zip);
        if (editing) {
          onUpdate(editing.id, buildUpdatePayload(v, editing), file ? { file, version: v.version } : undefined);
        } else if (isUpload) {
          onCreateUpload(buildUploadMeta(v), file!);
        } else {
          onCreate(buildCreatePayload(v));
        }
      })
      .catch((err: { errorFields?: { name: (string | number)[] }[] }) => {
        // field errors are shown on the form; make sure a hidden one is visible
        if (err.errorFields?.some((f) => ADVANCED_FIELDS.includes(String(f.name[0])))) setAdvancedOpen(["advanced"]);
      });

  const zipField = (
    <Form.Item
      name="zip"
      label={editing ? "New zip" : "Source zip"}
      valuePropName="fileList"
      getValueFromEvent={(e: { fileList: UploadFile[] }) => e.fileList.slice(-1)}
      rules={editing ? [] : [{ required: true, type: "array", min: 1, message: "Choose a .zip file" }]}
      style={{ marginBottom: 12 }}
    >
      <Upload.Dragger accept=".zip" maxCount={1} beforeUpload={() => false} style={{ padding: "4px 0" }}>
        <Space direction="vertical" size={2}>
          <Space size={8}>
            <InboxOutlined style={{ fontSize: 20 }} />
            <Text>Click or drag a .zip here</Text>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Up to 200 MB · node_modules, .git, venv and build folders are skipped
          </Text>
        </Space>
      </Upload.Dragger>
    </Form.Item>
  );

  const versionField = (
    <Form.Item
      name="version"
      label="Version"
      tooltip={
        editing
          ? 'A new label adds a version; an existing one replaces its zip. Blank = "upload".'
          : 'Label for this zip. Leave blank to use "upload".'
      }
    >
      <Input placeholder="e.g. 6.1.0" />
    </Form.Item>
  );

  return (
    <Modal
      title={editing ? `Edit ${editing.id}` : "Add repository"}
      open={open}
      width={640}
      onCancel={onCancel}
      confirmLoading={confirmLoading}
      okText={editing ? "Save" : "Add and scan"}
      destroyOnHidden
      // long forms scroll inside the dialog; the title and buttons stay put
      styles={{ body: { maxHeight: "calc(100vh - 240px)", overflowY: "auto", overflowX: "hidden", paddingRight: 8 } }}
      onOk={submit}
    >
      <Form
        // a new form per repo (and for Add), so no values carry over between them
        key={editing?.id ?? "__new__"}
        form={form}
        layout="vertical"
        requiredMark="optional"
        preserve={false}
        initialValues={initialValues}
      >
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}

        {!editing && (
          <Segmented
            block
            style={{ marginBottom: 20 }}
            value={addMode}
            onChange={(v) => setAddMode(v as "git" | "upload")}
            options={[
              { value: "git", label: "Git repository", icon: <BranchesOutlined /> },
              { value: "upload", label: "Upload zip", icon: <InboxOutlined /> },
            ]}
          />
        )}

        <SectionTitle>Source</SectionTitle>
        {isUpload ? (
          editing ? (
            <>
              <Form.Item
                name="branches"
                label="Versions"
                tooltip="Each version keeps its own history. Removing one deletes its zip."
                rules={[{ required: true, type: "array", min: 1, message: "At least one version is required" }]}
              >
                <Select mode="multiple" options={editing.branches.map((b) => ({ value: b, label: b }))} />
              </Form.Item>
              {zipField}
              {versionField}
            </>
          ) : (
            <>
              <Row gutter={12}>
                <Col xs={24} sm={14}>
                  <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true, message: "Enter a name" }]}>
                    <Input placeholder="e.g. web-ui" autoFocus />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={10}>
                  {versionField}
                </Col>
              </Row>
              {zipField}
            </>
          )
        ) : (
          <>
            <Form.Item name="url" label="Repository URL" rules={[{ required: true, message: "Enter the repository URL" }]}>
              <Input placeholder="https://gitlab.com/group/project" disabled={!!editing} autoFocus={!editing} />
            </Form.Item>
            <Form.Item
              name="branches"
              label="Branches"
              tooltip="Each branch is scanned separately and keeps its own history."
              rules={[{ required: true, type: "array", min: 1, message: "At least one branch is required" }]}
            >
              <Select mode="tags" tokenSeparators={[","]} placeholder="Type a branch name and press Enter" open={false} suffixIcon={null} />
            </Form.Item>
          </>
        )}

        <SectionTitle>Schedule</SectionTitle>
        <Row gutter={12}>
          <Col xs={24} sm={14}>
            <Form.Item
              name="release_date"
              label="Planned release date"
              tooltip="Packages that reach end of life before (or soon after) this date are flagged. Changing it re-checks results instantly."
            >
              <DatePicker style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col xs={24} sm={10}>
            <Form.Item name="interval_days" label="Scan every" rules={[{ required: true, message: "Required" }]}>
              <InputNumber min={1} style={{ width: "100%" }} suffix="days" />
            </Form.Item>
          </Col>
        </Row>

        <Collapse
          ghost
          activeKey={advancedOpen}
          onChange={(k) => setAdvancedOpen(Array.isArray(k) ? k : [k])}
          style={{ margin: "0 -16px" }}
          items={[
            {
              key: "advanced",
              // keep the fields mounted while collapsed so their values are saved
              forceRender: true,
              label: (
                <Space size={8}>
                  <Text strong>Advanced</Text>
                  {advancedSummary && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {advancedSummary}
                    </Text>
                  )}
                </Space>
              ),
              children: (
                <>
                  <Form.Item
                    name="ignored_packages"
                    label="Ignored packages"
                    tooltip="Shown as Ignored and never counted as a risk, e.g. test-only packages. Applies to every branch."
                  >
                    <Select mode="tags" tokenSeparators={[",", " "]} placeholder="Type a package name and press Enter" open={false} suffixIcon={null} />
                  </Form.Item>
                  <Form.Item
                    name="image_tags_text"
                    label="Docker image tags"
                    tooltip="For compose files whose images come from variables, e.g. image: ${CORE_TAG}. One VAR=image:tag per line."
                    extra={
                      detected.length > 0 && (
                        <Text type={missingTags.length ? "warning" : "secondary"} style={{ fontSize: 12 }}>
                          The last scan needs: <code>{detected.join(", ")}</code>
                          {missingTags.length > 0 && ` (missing ${missingTags.join(", ")})`}
                        </Text>
                      )
                    }
                    rules={[{ validator: async (_r, value) => void textToTags(value) }]}
                  >
                    <Input.TextArea
                      autoSize={{ minRows: 2, maxRows: 6 }}
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="CORE_TAG=registry/myapp:core-6.1.0"
                      style={{ fontFamily: "monospace", fontSize: 12 }}
                    />
                  </Form.Item>
                  {!isUpload && (
                    <Form.Item
                      name="token"
                      label="Repository token"
                      tooltip="Only needed when the token in .env can't reach this repository. Never shown again after saving."
                      extra={hasStoredToken ? "A token is saved. Leave blank to keep it." : undefined}
                      style={hasStoredToken ? { marginBottom: 8 } : undefined}
                    >
                      <Input.Password
                        placeholder={hasStoredToken ? "Type a new token to replace it" : "Leave blank to use the token from .env"}
                        autoComplete="new-password"
                        disabled={!!removeToken}
                      />
                    </Form.Item>
                  )}
                  {!isUpload && hasStoredToken && (
                    <Form.Item name="remove_token" valuePropName="checked" style={{ marginBottom: 0 }}>
                      <Checkbox>Remove the saved token</Checkbox>
                    </Form.Item>
                  )}
                </>
              ),
            },
          ]}
        />
      </Form>
    </Modal>
  );
}
