Feature: Cancelling a step
  As a program acting on a user's message
  I want to record that a step is no longer wanted without touching its ticket
  So that the agent who owns the step keeps the last word on it

  Background:
    Given a clean tickets directory
    And a step record exists with ID "wor-step-0001" and title "Add a quiz"

  Scenario: Cancel writes a sidecar and leaves the ticket alone
    When I run "ticket cancel wor-step-0001 'you called off the quiz'"
    Then the command should succeed
    And the output should be "Cancelled wor-step-0001: you called off the quiz"
    And ticket "wor-step-0001" should have a cancel sidecar with reason "you called off the quiz"
    And ticket "wor-step-0001" should not contain "cancel"

  Scenario: Cancel requires a reason
    When I run "ticket cancel wor-step-0001"
    Then the command should fail

  Scenario: A cancel marks the step in the steps list and in show
    Given ticket "wor-step-0001" was started long ago
    When I run "ticket cancel wor-step-0001 'no quiz'"
    And I run "ticket steps --all-agents"
    Then the output should contain "Add a quiz -- cancelled by the user's message: no quiz"
    When I run "ticket show wor-step-0001"
    Then the output should contain "Cancelled by the user's message: no quiz"

  Scenario: Starting the step after a cancel overrides it
    When I run "ticket cancel wor-step-0001 'no quiz'"
    And I run "ticket start wor-step-0001"
    Then the command should succeed
    And the output should contain "overrides its cancel (no quiz)"
    When I run "ticket steps --all-agents"
    Then the output should not contain "cancelled by"

  Scenario: Closing the step after a cancel keeps the cancel
    Given ticket "wor-step-0001" was started long ago
    When I run "ticket cancel wor-step-0001 'no quiz'"
    And I run "ticket close wor-step-0001 'No longer needed'"
    And I run "ticket steps --all-agents --status=closed"
    Then the output should contain "cancelled by the user's message: no quiz"

  Scenario: A later cancel gets its own sidecar, and counts
    Given ticket "wor-step-0001" was started long ago
    When I run "ticket cancel wor-step-0001 'first'"
    And I run "ticket cancel wor-step-0001 'second'"
    Then the command should succeed
    When I run "ticket steps --all-agents"
    Then the output should contain "cancelled by the user's message: second"

  Scenario: Create with a given id never writes over an existing ticket
    When I run "ticket create --step 'Other' --id wor-step-0001"
    Then the command should fail
    And ticket "wor-step-0001" should contain "Add a quiz"
