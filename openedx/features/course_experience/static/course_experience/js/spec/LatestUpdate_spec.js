/* globals $, loadFixtures */

import 'jquery.cookie';
import { LatestUpdate } from '../LatestUpdate';

describe('LatestUpdate tests', () => {
  describe('Test cookies', () => {
    beforeEach(() => {
      loadFixtures('course_experience/fixtures/latest-update-fragment.html');
      new LatestUpdate({ messageContainer: '.update-message', dismissButton: '.dismiss-message button' }); // eslint-disable-line no-new
    });

    it('Test setting cookie', () => {
      $('.dismiss-message button').click();
      expect($('.update-message').attr('style')).toBe('display: none;');

      // Check that the cookie still applies for a new latest update.
      $('.update-message').show();
      new LatestUpdate({ messageContainer: '.update-message', dismissButton: '.dismiss-message button' }); // eslint-disable-line no-new
      expect($('.update-message').attr('style')).toBe('display: none;');
    });
  });
});
