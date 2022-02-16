import torch.nn as nn
from tqdm import tqdm
import numpy as np
import torch
from torch.nn.modules import Module
import logging
from torch.optim import Adam
import time
from pathlib import Path
from .evaluator import gap_eval_scores

def print_network(net, verbose=False):
    num_params = 0
    for i, param in enumerate(net.parameters()):
        num_params += param.numel()
    if verbose:
        logging.info(net)
    logging.info('Total number of parameters: %d\n' % num_params)


def save_checkpoint(
    epoch, epochs_since_improvement, model, loss, 
    dev_predictions, test_predictions, dev_evaluations, 
    test_evaluations, is_best, checkpoint_dir):

    _state = {
        'epoch': epoch,
        'epochs_since_improvement': epochs_since_improvement,
        'model': model,
        'loss': loss,
        'dev_predictions': dev_predictions,
        'test_predictions': test_predictions,
        'dev_evaluations': dev_evaluations,
        'test_evaluations': test_evaluations
        }

    filename = 'checkpoint_' + "epoch{}".format(epoch) + '.pth.tar'
    torch.save(_state, Path(checkpoint_dir) / filename)
    # If this checkpoint is the best so far, store a copy so it doesn't get overwritten by a worse checkpoint
    if is_best:
        torch.save(_state, Path(checkpoint_dir) / 'BEST_' + filename)

# train the main model with adv loss
def train_epoch(model, iterator, args, epoch, discriminator = None, staring_adv = False):

    epoch_loss = 0
    model.train()

    optimizer = model.optimizer
    criterion = model.criterion

    data_t0 = time.time()
    
    for it, batch in enumerate(iterator):

        data_t = time.time() - data_t0
        t0 = time.time()
        
        text = batch[0]
        tags = batch[1].long()
        p_tags = batch[2].float()

        if args.BT is not None and args.BT == "Reweighting":
            instance_weights = batch[3].float()
            instance_weights = instance_weights.to(args.device)

        text = text.to(args.device)
        tags = tags.to(args.device)
        p_tags = p_tags.to(args.device)
        
        optimizer.zero_grad()
        # main model predictions
        predictions = model(text)
        # main tasks loss
        # add the weighted loss
        if args.BT is not None and args.BT == "Reweighting":
            loss = criterion(predictions, tags)
            loss = torch.mean(loss * instance_weights)
        else:
            loss = criterion(predictions, tags)

        # if (args.adv and staring_adv):
        #     # discriminator predictions
        #     p_tags = p_tags.long()

        #     hs = model.hidden(text)

        #     if args.gate_adv:
        #         adv_predictions = discriminator(hs, tags)
        #     else:
        #         adv_predictions = discriminator(hs)

        #     if uniform_adv_loss:
        #         # uniform labels
        #         batch_size, num_g_class = adv_predictions.shape
        #         # init uniform protected attributes
        #         p_tags = (1/num_g_class) * torch.ones_like(adv_predictions)
        #         p_tags = p_tags.to(device)
        #         # calculate the adv loss with the uniform protected labels
        #         # cross entropy loss for soft labels
        #         adv_loss = cross_entropy_with_probs(adv_predictions, p_tags)
        #     else:
        #         # add the weighted loss
        #         if args.adv_weighting is not None:
        #             adv_loss = adv_criterion(adv_predictions, p_tags)
        #             adv_loss = torch.mean(adv_loss)
        #         else:
        #             adv_loss = adv_criterion(adv_predictions, p_tags)
            
        #     loss = loss + adv_loss
        loss.backward()
        optimizer.step()
        epoch_loss += loss.item()
        t = time.time() - t0
        data_t0 = time.time()

        if it % args.log_interval == 0:
            logging.info((
                    'Epoch: {:4d} [{:7d}/{:7d} ({:2.0f}%)]\tLoss: {:.4f}\t'
                    'Data Time: {:.2f}s\tTrain Time: {:.2f}s'
                ).format(
                    epoch, it * args.batch_size, len(iterator.dataset),
                    100. * it / len(iterator), loss, data_t, t,
                ))
        
    return epoch_loss / len(iterator)


# to evaluate the main model
def eval_epoch(model, iterator, args):
    
    epoch_loss = 0
    device = args.device
    
    model.eval()

    criterion = model.criterion

    preds = []
    labels = []
    private_labels = []

    for batch in iterator:
        
        text = batch[0]

        tags = batch[1]
        p_tags = batch[2]

        text = text.to(device)
        tags = tags.to(device).long()
        p_tags = p_tags.to(device).float()

        if args.BT is not None and args.BT == "Reweighting":
            instance_weights = batch[3].float()
            instance_weights = instance_weights.to(device)

        predictions = model(text)
        
        # add the weighted loss
        if args.BT is not None and args.BT == "Reweighting":
            loss = criterion(predictions, tags)
            loss = torch.mean(loss * instance_weights)
        else:
            loss = criterion(predictions, tags)
                        
        epoch_loss += loss.item()
        
        predictions = predictions.detach().cpu()
        tags = tags.cpu().numpy()

        preds += list(torch.argmax(predictions, axis=1).numpy())
        labels += list(tags)

        private_labels += list(batch[2].cpu().numpy())
    
    return ((epoch_loss / len(iterator)), preds, labels, private_labels)

class BaseModel(nn.Module):

    def init_for_training(self):

        self.device = self.args.device
        self.to(self.device)

        self.learning_rate = self.args.lr
        self.optimizer = Adam(filter(lambda p: p.requires_grad, self.parameters()), lr=self.learning_rate)

        if self.args.BT and self.args.BT == "Reweighting":
            self.criterion = torch.nn.CrossEntropyLoss(reduction = "none")
        else:
            self.criterion = torch.nn.CrossEntropyLoss()
        
        print_network(self, verbose=True)

    def init_hyperparameters(self):
        if self.args.activation_function == "ReLu":
            self.AF = nn.ReLU()
        elif self.args.activation_function == "Tanh":
            self.AF = nn.Tanh()
        elif self.args.activation_function == "LeakyReLU":
            self.AF = nn.LeakyReLU()
        else:
            raise "not implemented yet"

        if self.args.batch_norm:
            self.BN = nn.BatchNorm1d(self.args.hidden_size)
        else:
            self.BN = None

        assert (self.args.dropout >= 0) and (self.args.dropout <= 1), "Probability must be in the range from 0 to 1"
        if self.args.dropout > 0:
            self.dropout = nn.Dropout(p=self.args.dropout)
        else:
            self.dropout = None
    
    def train_self(self):
        epochs_since_improvement = 0
        best_valid_loss = 1e+5

        for epoch in range(self.args.opt.epochs):
            
            # Early stopping
            if epochs_since_improvement >= self.args.epochs_since_improvement:
                break
            
            # One epoch's training
            epoch_train_loss = train_epoch(
                model = self, 
                iterator = self.args.opt.train_generator, 
                args = self.args, 
                epoch = epoch, 
                discriminator = None, 
                staring_adv = False)

            # One epoch's validation
            (epoch_valid_loss, valid_preds, 
            valid_labels, valid_private_labels) = eval_epoch(
                model = self, 
                iterator = self.args.opt.dev_generator, 
                args = self.args)

            # Check if there was an improvement
            is_best = epoch_valid_loss > best_valid_loss
            best_loss = min(epoch_valid_loss, best_valid_loss)

            if not is_best:
                epochs_since_improvement += 1
                logging.info("Epochs since last improvement: %d" % (epochs_since_improvement,))
            else:
                epochs_since_improvement = 0

            if epoch % self.args.checkpoint_interval == 0:
                valid_scores = gap_eval_scores(
                    y_pred=valid_preds,
                    y_true=valid_labels, 
                    protected_attribute=valid_private_labels)

                (epoch_test_loss, test_preds, 
                test_labels, test_private_labels) = eval_epoch(
                    model = self, 
                    iterator = self.args.opt.test_generator, 
                    args = self.args)
                
                test_scores = gap_eval_scores(
                    y_pred=test_preds,
                    y_true=test_labels, 
                    protected_attribute=test_private_labels)

                # Save checkpoint
                save_checkpoint(
                    epoch = epoch, 
                    epochs_since_improvement = epochs_since_improvement, 
                    model = self, 
                    loss = epoch_valid_loss, 
                    dev_predictions = valid_preds, 
                    test_predictions = test_preds,
                    dev_evaluations = valid_scores, 
                    test_evaluations = test_scores,
                    is_best = is_best,
                    checkpoint_dir = self.args.model_dir)
                
                logging.info("Evaluation at Epoch %d" % (epoch,))
                logging.info((
                    'Validation GAP: {:2.0f} \tAcc: {:2.0f} \tMacroF1: {:2.0f} \tMicroF1: {:2.0f} \t'
                ).format(
                    100. * valid_scores["rms_TPR"], 100. * valid_scores["accuracy"], 
                    100. * valid_scores["macro_fscore"], 100. * valid_scores["micro_fscore"]
                ))
                logging.info((
                    'Test GAP: {:2.0f} \tAcc: {:2.0f} \tMacroF1: {:2.0f} \tMicroF1: {:2.0f} \t'
                ).format(
                    100. * test_scores["rms_TPR"], 100. * test_scores["accuracy"], 
                    100. * test_scores["macro_fscore"], 100. * test_scores["micro_fscore"]
                ))