import unittest
from research.labeling.barriers import triple_barrier
from research.models.logistic import Logistic, evaluate, score


class ModelTests(unittest.TestCase):
    def test_scaler_fits_training_only(self):
        model=Logistic().fit([[1],[2],[3],[4]],[0,0,1,1])
        self.assertEqual(model.means,[2.5])
        old=list(model.weights)
        model.predict([[10000]])
        self.assertEqual(model.means,[2.5])
        self.assertEqual(model.weights,old)

    def test_single_class_refused(self):
        with self.assertRaises(ValueError): Logistic().fit([[1],[2]],[1,1])

    def test_future_test_label_does_not_change_predictions(self):
        train=[{'x':[i/10],'y':i%2} for i in range(30)]
        validation=[{'x':[i/10],'y':i%2} for i in range(30,40)]
        a=evaluate(train,validation,[{'x':[4.1],'y':0}])
        b=evaluate(train,validation,[{'x':[4.1],'y':1}])
        self.assertEqual(a['test_predictions'][0]['probability'],b['test_predictions'][0]['probability'])
        self.assertEqual(a['model_artifact']['training_sha256'],b['model_artifact']['training_sha256'])
        self.assertEqual(a['calibration_artifact']['training_sha256'],b['calibration_artifact']['training_sha256'])
        self.assertEqual(a['train_scaler_means'],b['train_scaler_means'])

    def test_brier_exact(self):
        self.assertEqual(score([.5,.5],[0,1])['brier'],.25)
        self.assertEqual(score([0,1],[0,1])['brier'],0)

    def test_ambiguous_bar_is_not_labeled_win(self):
        bars=[{'low':98,'high':102,'close':101,'time_close':1000}]
        result=triple_barrier(bars,0,1,100,99,101)
        self.assertIsNone(result['label'])
        self.assertEqual(result['status'],'AMBIGUOUS_SAME_BAR')
        self.assertEqual(result['available_at'],1000)

    def test_censored_outcome_remains_unknown(self):
        bars=[{'low':99.5,'high':100.5,'close':100.2,'time_close':1000}]
        self.assertEqual(triple_barrier(bars,0,1,100,99,101)['status'],'CENSORED')

    def test_short_barrier_side(self):
        bars=[{'low':98,'high':100.5,'close':99,'time_close':1000}]
        self.assertEqual(triple_barrier(bars,0,-1,100,101,99)['label'],1)


if __name__=='__main__': unittest.main()
