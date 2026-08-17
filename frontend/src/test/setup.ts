import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom does not implement Element.scrollTo. Stubbed here rather than guarded
// in the component: the call is correct in a real browser, and weakening
// application code to satisfy a test environment is the wrong direction.
Element.prototype.scrollTo = () => {};

// Unmount between tests so a leftover tree cannot satisfy the next assertion.
afterEach(cleanup);
